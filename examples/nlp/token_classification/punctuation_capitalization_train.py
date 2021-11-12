# Copyright (c) 2021, NVIDIA CORPORATION & AFFILIATES.  All rights reserved.
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

import os

import pytorch_lightning as pl
import torch
from omegaconf import DictConfig, OmegaConf

from nemo.collections.nlp.models import PunctuationCapitalizationModel
from nemo.collections.nlp.models.token_classification.punctuation_capitalization_config import (
    PunctuationCapitalizationConfig,
)
from nemo.core.config import hydra_runner
from nemo.utils import logging
from nemo.utils.config_utils import update_model_config
from nemo.utils.exp_manager import exp_manager


"""
This script show how to train a Punctuation and Capitalization Model.
More details on the task and data format could be found in tutorials/nlp/Punctuation_and_Capitalization.ipynb

*** Setting the configs ***

The model and the PT trainer are defined in a config file which declares multiple important sections.
The most important ones are:
    model: All arguments that are related to the Model - language model, tokenizer, token classifier, optimizer,
            schedulers, and datasets/data loaders.
    trainer: Any argument to be passed to PyTorch Lightning including number of epochs, number of GPUs,
            precision level, etc.
This script uses the `/examples/nlp/token_classification/conf/punctuation_capitalization_config.yaml` config file
by default. You may update the config file from the file directly. 
The other option is to set another config file via command line arguments by `--config-name=CONFIG_FILE_PATH'.

For more details about the config files and different ways of model restoration, see tutorials/00_NeMo_Primer.ipynb

*** Model training ***

To run this script and train the model from scratch, use:
    python punctuation_and_capitalization_train.py \
           model.dataset.data_dir=<PATH_TO_DATA_DIR>

To use one of the pretrained versions of the model and finetune it, run:
    python punctuation_and_capitalization.py \
    pretrained_model=punctuation_en_bert \
    model.dataset.data_dir=<PATH_TO_DATA_DIR>
    
    <PATH_TO_DATA_DIR> - a directory that contains test_ds.text_file and test_ds.labels_file (see the config)
    pretrained_model   - pretrained PunctuationCapitalization model from list_available_models() or 
                     path to a .nemo file, for example: punctuation_en_bert or model.nemo

"""


@hydra_runner(config_path="conf", config_name="punctuation_capitalization_config")
def main(cfg: DictConfig) -> None:
    torch.manual_seed(42)
    default_cfg = PunctuationCapitalizationConfig()
    cfg = update_model_config(default_cfg, cfg)
    trainer = pl.Trainer(**cfg.trainer)
    exp_manager(trainer, cfg.get("exp_manager", None))

    if not cfg.pretrained_model:
        logging.info(f'Config: {OmegaConf.to_yaml(cfg)}')
        model = PunctuationCapitalizationModel(cfg.model, trainer=trainer)
    else:
        if os.path.exists(cfg.pretrained_model):
            model = PunctuationCapitalizationModel.restore_from(cfg.pretrained_model)
        elif cfg.pretrained_model in PunctuationCapitalizationModel.get_available_model_names():
            model = PunctuationCapitalizationModel.from_pretrained(cfg.pretrained_model)
        else:
            raise ValueError(
                f'Provide path to the pre-trained .nemo file or choose from {PunctuationCapitalizationModel.list_available_models()}'
            )
        model.update_config(
            dataset=cfg.model.dataset,
            train_ds=cfg.model.train_ds,
            validation_ds=cfg.model.validation_ds,
            test_ds=cfg.model.test_ds,
            optim=cfg.model.optim,
        )
        model.set_trainer(trainer)
        model.setup_training_data()
        model.setup_validation_data()
        model.setup_optimization()

    trainer.fit(model)


if __name__ == '__main__':
    main()
