# Copyright (c) 2022, NVIDIA CORPORATION.  All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""
# Training the model
```sh
CUDA_VISIBLE_DEVICE=0 python speech_separation.py \
    --config-path=<path to dir of configs> --config-name=<name of config without .yaml> \
    model.train_ds.manifest_filepath=<path to train manifest> \
    model.validation_ds.manifest_filepath=<path to val/test manifest> \
    trainer.max_epochs=100 \
    exp_manager.create_wandb_logger=True \
    exp_manager.wandb_logger_kwargs.name="<Name of experiment>" \
    exp_manager.wandb_logger_kwargs.project="<Name of project>"
```
"""

import pytorch_lightning as pl
import torch
from omegaconf import OmegaConf

from nemo.collections.asr.models.tss_model import TargetEncDecSpeechSeparationModel
from nemo.core.config import hydra_runner
from nemo.utils import logging
from nemo.utils.exp_manager import exp_manager


@hydra_runner(config_path="./conf/", config_name="target_sep_transformer")
def main(cfg):
    logging.info(f'Hydra config: {OmegaConf.to_yaml(cfg)}')

    assert (
        cfg.model.train_ds.batch_size == 1
    ), "currently supports only batch_size=1, similar to https://arxiv.org/pdf/2010.13154.pdf"

    trainer = pl.Trainer(**cfg.trainer)
    exp_manager(trainer, cfg.get("exp_manager", None))
    sep_model = TargetEncDecSpeechSeparationModel(cfg=cfg.model, trainer=trainer)

    # Initialize the weights of the model from another model, if provided via config
    sep_model.maybe_init_from_pretrained_checkpoint(cfg)

    if cfg.train:
        trainer.fit(sep_model)

    if hasattr(cfg.model, 'test_ds') and cfg.model.test_ds.manifest_filepath is not None:
        if sep_model.prepare_test(trainer):
            trainer.test(sep_model)


if __name__ == '__main__':
    main()
