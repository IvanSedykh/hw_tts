import argparse
import json
import os
from pathlib import Path
import datetime

import torch
from tqdm import tqdm
import numpy as np

import hw_asr.model as module_model
from hw_asr.trainer import Trainer
from hw_asr.utils import ROOT_PATH
from hw_asr.utils.object_loading import get_dataloaders
from hw_asr.utils.parse_config import ConfigParser
from hw_asr.metric.utils import calc_cer, calc_wer


# fix random seeds for reproducibility
SEED = 0xdeadbeef
torch.manual_seed(SEED)
np.random.seed(SEED)

DEFAULT_CHECKPOINT_PATH = ROOT_PATH / "default_test_model" / "checkpoint.pth"
NUM_BEAMS = 30


def calc_mean_metric(gt_list: list[str], pred_list:list[str], metric_func: callable):
    vals = []
    for target, pred in zip(gt_list, pred_list):
        vals.append(metric_func(target, pred))
    return sum(vals) / len(vals)


def main(config, out_file):
    logger = config.get_logger("test")

    # define cpu or gpu if possible
    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    print(f"Using {device}")

    # text_encoder
    text_encoder = config.get_text_encoder()

    # setup data_loader instances
    print(f"Loading data...")
    dataloaders = get_dataloaders(config, text_encoder)
    print(dataloaders['test'].batch_size)

    # build model architecture
    print(f"Building model...")
    model = config.init_obj(config["arch"], module_model, n_class=len(text_encoder))
    logger.info(model)

    logger.info("Loading checkpoint: {} ...".format(config.resume))
    checkpoint = torch.load(config.resume, map_location=device)
    state_dict = checkpoint["state_dict"]
    if config["n_gpu"] > 1:
        model = torch.nn.DataParallel(model)
    model.load_state_dict(state_dict)

    # prepare model for testing
    model = model.to(device)
    model.eval()

    results = []

    with torch.no_grad():
        for batch_num, batch in enumerate(tqdm(dataloaders["test"])):
            batch = Trainer.move_batch_to_device(batch, device)
            output = model(**batch)
            if type(output) is dict:
                batch.update(output)
            else:
                batch["logits"] = output

            # we dont need probs/log probs. logits are ok
            # batch["log_probs"] = torch.log_softmax(batch["logits"], dim=-1)
            # batch["probs"] = batch["log_probs"].exp().cpu()
            # batch["argmax"] = batch["probs"].argmax(-1)
            batch["log_probs_length"] = model.transform_input_lengths(
                batch["spectrogram_length"]
            )
            batch["argmax"] = batch["logits"].argmax(-1)

            # BATCHED BEAM SEARCH ########
            pred_bs_lm = text_encoder.batched_ctc_beam_search_lm(batch, NUM_BEAMS)
            pred_bs = text_encoder.batched_ctc_beam_search(batch, NUM_BEAMS)
            # ###########
            for i in range(len(batch["text"])):
                argmax = batch["argmax"][i]
                argmax = argmax[: int(batch["log_probs_length"][i])]
                results.append(
                    {
                        "ground_trurh": batch["text"][i],
                        "pred_text_argmax": text_encoder.ctc_decode(argmax.cpu().numpy()),
                        # "pred_text_beam_search": text_encoder.ctc_beam_search(
                        #     batch["logits"][i], batch["log_probs_length"][i], beam_size=NUM_BEAMS
                        # )[:10],
                        "pred_text_beam_search": pred_bs[i][:10],
                        # "pred_text_beam_search_lm": text_encoder.ctc_beam_search_lm(
                        #     batch["logits"][i], batch["log_probs_length"][i], beam_size=NUM_BEAMS
                        # )[:10],
                        "pred_text_beam_search_lm": pred_bs_lm[i][:10]
                    }
                )
    with Path(out_file).open("w") as f:
        json.dump(results, f, indent=2)

    argmax_wer = calc_mean_metric(
        [i['ground_trurh'] for i in results],
        [i['pred_text_argmax'] for i in results],
        calc_wer
    )
    print(f"WER (ARGMAX): {argmax_wer:.2%}")

    bs_wer = calc_mean_metric(
        [i['ground_trurh'] for i in results],
        [i['pred_text_beam_search'][0][0] for i in results],
        calc_wer
    )
    print(f"WER (BS): {bs_wer:.2%}")

    bs_lm_wer = calc_mean_metric(
        [i['ground_trurh'] for i in results],
        [i['pred_text_beam_search_lm'][0][0] for i in results],
        calc_wer
    )
    print(f"WER (BS + LM): {bs_lm_wer:.2%}")


if __name__ == "__main__":
    args = argparse.ArgumentParser(description="PyTorch Template")
    args.add_argument(
        "-c",
        "--config",
        default=None,
        type=str,
        help="config file path (default: None)",
    )
    args.add_argument(
        "-r",
        "--resume",
        default=str(DEFAULT_CHECKPOINT_PATH.absolute().resolve()),
        type=str,
        help="path to latest checkpoint (default: None)",
    )
    args.add_argument(
        "-d",
        "--device",
        default=None,
        type=str,
        help="indices of GPUs to enable (default: all)",
    )
    args.add_argument(
        "-o",
        "--output",
        default="output.json",
        type=str,
        help="File to write results (.json)",
    )
    args.add_argument(
        "-t",
        "--test-data-folder",
        default=None,
        type=str,
        help="Path to dataset",
    )
    args.add_argument(
        "-b",
        "--batch-size",
        default=20,
        type=int,
        help="Test dataset batch size",
    )
    args.add_argument(
        "-j",
        "--jobs",
        default=1,
        type=int,
        help="Number of workers for test dataloader",
    )

    args = args.parse_args()

    # set GPUs
    if args.device is not None:
        os.environ["CUDA_VISIBLE_DEVICES"] = args.device

    # first, we need to obtain config with model parameters
    # we assume it is located with checkpoint in the same folder
    model_config = Path(args.resume).parent / "config.json"
    with model_config.open() as f:
        config = ConfigParser(json.load(f), resume=args.resume)

    # update with addition configs from `args.config` if provided
    if args.config is not None:
        with Path(args.config).open() as f:
            config.config.update(json.load(f))

    # if `--test-data-folder` was provided, set it as a default test set
    if args.test_data_folder is not None:
        test_data_folder = Path(args.test_data_folder).absolute().resolve()
        assert test_data_folder.exists()
        config.config["data"] = {
            "test": {
                "batch_size": args.batch_size,
                "num_workers": args.jobs,
                "datasets": [
                    {
                        "type": "CustomDirAudioDataset",
                        "args": {
                            "audio_dir": str(test_data_folder / "audio"),
                            "transcription_dir": str(
                                test_data_folder / "transcriptions"
                            ),
                        },
                    }
                ],
            }
        }

    assert config.config.get("data", {}).get("test", None) is not None
    config["data"]["test"]["batch_size"] = args.batch_size
    config["data"]["test"]["n_jobs"] = args.jobs

    main(config, args.output)
