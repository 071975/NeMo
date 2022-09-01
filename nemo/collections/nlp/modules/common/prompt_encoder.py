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

import enum
from typing import Dict, Optional

import torch
import torch.nn.functional as F
from torch import nn

from nemo.collections.nlp.modules.common.megatron.fused_bias_gelu import fused_bias_gelu
from nemo.collections.nlp.modules.common.megatron.utils import ApexGuardDefaults, init_method_normal
from nemo.core.classes import Exportable, NeuralModule
from nemo.core.classes.common import typecheck
from nemo.core.neural_types import ChannelType, NeuralType
from nemo.utils import logging

try:
    from apex.transformer import tensor_parallel, parallel_state

    HAVE_APEX = True

except (ImportError, ModuleNotFoundError):
    HAVE_APEX = False

    # fake missing classes with None attributes
    ModelType = AttnMaskType = AttnType = LayerType = ApexGuardDefaults()


__all__ = ['PromptEncoder']


class PromptEncoderType(enum.Enum):
    LSTM = 'lstm'
    TPMLP = 'tpmlp'


class PromptEncoder(NeuralModule, Exportable):
    """
    The prompt encoder network that is used to generate the virtual 
    token embeddings for p-tuning.
    """

    @property
    def input_types(self) -> Optional[Dict[str, NeuralType]]:
        return {
            "taskname_embeddings": NeuralType(('B', 'T', 'C'), ChannelType(), optional=False),
        }

    @property
    def output_types(self) -> Optional[Dict[str, NeuralType]]:
        return {"output_embeds": NeuralType(('B', 'T', 'C'), ChannelType())}

    def __init__(
        self, total_virtual_tokens: int, hidden_size: int, output_size: int, lstm_dropout: float, num_layers: int
    ):
        """
        Initializes the PromptEncoder module.
        Args:
            total_virtual_tokens: the total number of vitural tokens
            hidden_size: hidden dimension
            lstm_dropout: the dropout used for the LSTM
            num_layers: number of layers used in the LSTM
        """
        super().__init__()
        self.hidden_size = hidden_size
        self.output_size = output_size
        self.total_virtual_tokens = total_virtual_tokens

        # Set fixed indicies for forward pass
        self.register_buffer('indices', torch.LongTensor(list(range(self.total_virtual_tokens))))

        # embedding
        self.embedding = torch.nn.Embedding(self.total_virtual_tokens, hidden_size)

        # LSTM
        self.lstm_head = torch.nn.LSTM(
            input_size=hidden_size,
            hidden_size=self.hidden_size // 2,
            num_layers=num_layers,
            dropout=lstm_dropout,
            bidirectional=True,
            batch_first=True,
        )
        self.mlp_head = nn.Sequential(
            nn.Linear(self.hidden_size, self.hidden_size), nn.ReLU(), nn.Linear(self.hidden_size, output_size)
        )

    @typecheck()
    def forward(self, taskname_embeddings) -> torch.Tensor:
        input_embeds = self.embedding(self.indices).unsqueeze(0)
        batch_size, task_seq_length, _ = taskname_embeddings.shape
        input_embeds = input_embeds.expand(batch_size, self.total_virtual_tokens, self.hidden_size).clone()
        length = min(task_seq_length, self.total_virtual_tokens)
        # need to adapt taskname embedding hidden to the same size as hidden_size
        taskname_embeddings = torch.matmul(taskname_embeddings, self.mlp_head[2].weight)
        # Replace general input with task specific embeddings to specify the correct task
        input_embeds[:, 0:length, :] = taskname_embeddings[:, 0:length, :]
        output_embeds = self.mlp_head(self.lstm_head(input_embeds)[0])
        return output_embeds


class PromptEncoderMLP(NeuralModule, Exportable):
    """
    The Tensor Parallel MLP prompt encoder network that is used to generate the virtual 
    token embeddings for p-tuning.
    """

    @property
    def input_types(self) -> Optional[Dict[str, NeuralType]]:
        return {
            "taskname_embeddings": NeuralType(('B', 'T', 'C'), ChannelType(), optional=False),
        }

    @property
    def output_types(self) -> Optional[Dict[str, NeuralType]]:
        return {"output_embeds": NeuralType(('B', 'T', 'C'), ChannelType())}

    def __init__(self, total_virtual_tokens: int, hidden_size: int, output_size: int, init_std: float):
        """
        Initializes the PromptEncoderMLP module.
        Args:
            total_virtual_tokens: the total number of vitural tokens
            hidden_size: hidden dimension
            lstm_dropout: the dropout used for the LSTM
            num_layers: number of layers used in the LSTM
        """
        super().__init__()
        self.hidden_size = hidden_size
        self.output_size = output_size
        self.total_virtual_tokens = total_virtual_tokens
        self.activation = 'gelu'
        sequence_parallel = False
        gradient_accumulation_fusion = False
        # Set fixed indicies for forward pass
        self.register_buffer('indices', torch.LongTensor(list(range(self.total_virtual_tokens))))

        # embedding
        self.embedding = torch.nn.Embedding(self.total_virtual_tokens, output_size)

        no_async_tensor_model_parallel_allreduce = (
            parallel_state.get_tensor_model_parallel_world_size() == 1 or sequence_parallel
        )
        self.first = tensor_parallel.ColumnParallelLinear(
            output_size,
            self.hidden_size,
            gather_output=False,
            init_method=init_method_normal(init_std),
            skip_bias_add=True,
            use_cpu_initialization=False,
            bias=True,
            sequence_parallel_enabled=sequence_parallel,
            no_async_tensor_model_parallel_allreduce=no_async_tensor_model_parallel_allreduce,
            gradient_accumulation_fusion=gradient_accumulation_fusion,
        )
        self.second = tensor_parallel.RowParallelLinear(
            self.hidden_size,
            output_size,
            input_is_parallel=True,
            init_method=init_method_normal(init_std),
            skip_bias_add=True,
            use_cpu_initialization=False,
            bias=True,
            sequence_parallel_enabled=sequence_parallel,
            gradient_accumulation_fusion=gradient_accumulation_fusion,
        )

    @typecheck()
    def forward(self, taskname_embeddings) -> torch.Tensor:
        input_embeds = self.embedding(self.indices).unsqueeze(0)
        batch_size, task_seq_length, _ = taskname_embeddings.shape
        input_embeds = input_embeds.expand(batch_size, self.total_virtual_tokens, self.output_size).clone()
        length = min(task_seq_length, self.total_virtual_tokens)
        # Replace general input with task specific embeddings to specify the correct task
        input_embeds[:, 0:length, :] = taskname_embeddings[:, 0:length, :]
        intermediate_parallel, bias_parallel = self.first(input_embeds)
        intermediate_parallel = fused_bias_gelu(intermediate_parallel, bias_parallel)
        output_embeds, bias_parallel = self.second(intermediate_parallel)
        output_embeds = output_embeds + bias_parallel
        return output_embeds
