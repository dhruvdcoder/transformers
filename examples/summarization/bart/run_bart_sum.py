import argparse
import glob
import logging
import os
import time

import torch
from torch.utils.data import DataLoader
from pathlib import Path

from transformer_base import BaseTransformer, add_generic_args, setup_trainer, get_linear_schedule_with_warmup
from utils import SummarizationDataset

logger = logging.getLogger(__name__)


class BartSystem(BaseTransformer):

    mode = "language-modeling"

    def __init__(self, hparams):
        super(BartSystem, self).__init__(
            hparams, num_labels=None, mode=self.mode)

    def forward(self,
                input_ids,
                attention_mask=None,
                decoder_input_ids=None,
                decoder_attention_mask=None,
                lm_labels=None):

        return self.model(
            input_ids,
            attention_mask=attention_mask,
            decoder_input_ids=decoder_input_ids,
            decoder_attention_mask=decoder_attention_mask,
            lm_labels=lm_labels,
        )

    def _step(self, batch):
        y = batch["target_ids"]
        y_ids = y[:, :-1].contiguous()
        lm_labels = y[:, 1:].clone()
        # Uncomment following line to ignore pad tokens in target while calculating loss
        #lm_labels[y[:, 1:] == self.tokenizer.pad_token_id] = -100
        target_mask = batch["target_mask"][:, :-1].contiguous(
        )  # drop one in mask as well
        outputs = self(
            input_ids=batch["source_ids"],
            attention_mask=batch["source_mask"],
            decoder_attention_mask=target_mask,
            decoder_input_ids=y_ids,
            lm_labels=lm_labels,
        )

        loss = outputs[0]

        return loss

    def training_step(self, batch, batch_idx):
        loss = self._step(batch)

        tensorboard_logs = {
            "train_loss": loss,
            "learning_rate": self.lr_scheduler.get_last_lr()[-1]
        }

        return {"loss": loss, "log": tensorboard_logs}

    def validation_step(self, batch, batch_idx):
        loss = self._step(batch)

        return {"val_loss": loss}

    def validation_end(self, outputs):
        avg_loss = torch.stack([x["val_loss"] for x in outputs]).mean()
        tensorboard_logs = {"val_loss": avg_loss}

        return {"avg_val_loss": avg_loss, "log": tensorboard_logs}

    def test_step(self, batch, batch_idx):
        generated_ids = self.model.generate(
            batch["source_ids"],
            attention_mask=batch["source_mask"],
            num_beams=1,
            do_sample=False,
            max_length=50,
            repetition_penalty=2.5,
            length_penalty=1.0,
            early_stopping=True,
        )
        preds = [
            self.tokenizer.decode(
                g, skip_special_tokens=True, clean_up_tokenization_spaces=True)

            for g in generated_ids
        ]
        target = [
            self.tokenizer.decode(
                t, skip_special_tokens=True, clean_up_tokenization_spaces=True)

            for t in batch["target_ids"]
        ]
        loss = self._step(batch)

        return {"val_loss": loss, "preds": preds, "target": target}

    def test_end(self, outputs):
        return self.validation_end(outputs)

    def test_epoch_end(self, outputs):
        output_test_predictions_file = os.path.join(self.hparams.output_dir,
                                                    "test_predictions.txt")
        output_test_targets_file = os.path.join(self.hparams.output_dir,
                                                "test_targets.txt")
        # write predictions and targets for later rouge evaluation.
        with open(output_test_predictions_file, "w+") as p_writer, open(
                output_test_targets_file, "w+") as t_writer:

            for output_batch in outputs:
                p_writer.writelines(s + "\n" for s in output_batch["preds"])
                t_writer.writelines(s + "\n" for s in output_batch["target"])
            p_writer.close()
            t_writer.close()

        return self.test_end(outputs)

    def train_dataloader(self):
        train_dataset = SummarizationDataset(
            self.tokenizer,
            data_dir=self.hparams.data_dir,
            type_path="train",
            block_size=self.hparams.max_seq_length)
        dataloader = DataLoader(
            train_dataset,
            batch_size=self.hparams.train_batch_size,
            shuffle=True)
        t_total = (
            (len(dataloader.dataset)
             // (self.hparams.train_batch_size * max(1, self.hparams.n_gpu)))
            // self.hparams.gradient_accumulation_steps * float(
                self.hparams.num_train_epochs))
        scheduler = get_linear_schedule_with_warmup(
            self.opt,
            num_warmup_steps=self.hparams.warmup_steps,
            num_training_steps=t_total)
        self.lr_scheduler = scheduler

        return dataloader

    def val_dataloader(self):
        val_dataset = SummarizationDataset(
            self.tokenizer,
            data_dir=self.hparams.data_dir,
            type_path="dev",
            block_size=self.hparams.max_seq_length)

        return DataLoader(val_dataset, batch_size=self.hparams.eval_batch_size)

    def test_dataloader(self):
        test_dataset = SummarizationDataset(
            self.tokenizer,
            data_dir=self.hparams.data_dir,
            type_path="test",
            block_size=self.hparams.max_seq_length)

        return DataLoader(
            test_dataset, batch_size=self.hparams.eval_batch_size)

    @staticmethod
    def add_model_specific_args(parser, root_dir):
        BaseTransformer.add_model_specific_args(parser, root_dir)
        # Add BART specific options
        parser.add_argument(
            "--max_seq_length",
            default=50,
            type=int,
            help="The maximum total input sequence length after tokenization. Sequences longer "
            "than this will be truncated, sequences shorter will be padded.",
        )

        parser.add_argument(
            "--data_dir",
            default=None,
            type=str,
            required=True,
            help="The input data dir. Should contain the dataset files for the CNN/DM summarization task.",
        )
        parser.add_argument(
            '--model_state',
            type=Path,
            help="Specify a .ckpt file to start training from that state."
            " Note: This not designed for resuming training from checkpoint but for doing pretraining/curriculum learning"
        )

        return parser


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    add_generic_args(parser, os.getcwd())
    parser = BartSystem.add_model_specific_args(parser, os.getcwd())
    args = parser.parse_args()

    # If output_dir not provided, a folder will be generated in pwd

    if args.output_dir is None:
        args.output_dir = os.path.join(
            "./results",
            f"{args.task}_{args.model_type}_{time.strftime('%Y%m%d_%H%M%S')}",
        )
        os.makedirs(args.output_dir)

    # load state if specified, else create fresh

    if args.do_train:
        if args.model_state is None:
            logger.info("Creating a fresh model")
            model = BartSystem(args)
        else:
            logger.info(
                f"Loading from {args.model_state}. Note: old config params will be ignored."
            )
            model = BartSystem(args)
            checkpoint = torch.load(
                args.model_state, map_location=lambda storage, loc: storage)
            model.load_state_dict(
                checkpoint['state_dict'])  # just load the state dict

        trainer = setup_trainer(args)
        trainer.fit(model)
    else:
        trainer = None

    # Optionally, predict on dev set and write to output_dir

    if args.do_predict:
        checkpoints = list(
            sorted(
                glob.glob(
                    os.path.join(args.output_dir, "epoch=*.ckpt"),
                    recursive=True)))
        logger.info(f"Predicting using {checkpoints[-1]}")
        model = BartSystem.load_from_checkpoint(checkpoints[-1])

        if trainer is None:
            trainer = setup_trainer(args)
        trainer.test(model)
