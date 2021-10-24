# Copyright (c) 2020, NVIDIA CORPORATION.  All rights reserved.
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

__all__ = ['BertPunctuationCapitalizationDataset', 'BertPunctuationCapitalizationInferDataset', 'Progress']

import itertools
import json
import multiprocessing as mp
import os
import pickle
import random
from math import ceil
from pathlib import Path
from queue import Empty
from time import sleep
from typing import Dict, List, Optional, Tuple, Union

import numpy as np
import torch
from torch.utils.data import IterableDataset
from torch.nn.utils.rnn import pad_sequence
from tqdm import tqdm

from nemo.collections.common.tokenizers.tokenizer_spec import TokenizerSpec
from nemo.collections.nlp.data.data_utils.data_preprocessing import get_label_stats, get_stats
from nemo.core.classes import Dataset
from nemo.core.neural_types import ChannelType, Index, LabelsType, MaskType, NeuralType
from nemo.core.neural_types.elements import BoolType
from nemo.utils import logging


MAX_NUM_QUERIES_IN_SPLIT = 10 ** 4
TOKENIZATION_PROGRESS_REPORT_PERIOD = 10 ** 3
BATCH_MARK_UP_PROGRESS_REPORT_PERIOD = 10 ** 4
BATCH_BUILDING_PROGRESS_REPORT_PERIOD = 10 ** 4


def check_number_of_labels(words, query, qi, split_i, punctuation_labels, capitalization_labels):
    if len(words) != len(punctuation_labels):
        raise ValueError(
            f"Number of punctuation labels for query {qi} in split {split_i} is not equal to number of "
            f"words. Number of words: {len(words)}, number of punctuation labels: "
            f"{len(punctuation_labels)}. Query: '{query}', punctuation labels: '{punctuation_labels}'"
        )
    if len(words) != len(capitalization_labels):
        raise ValueError(
            f"Number of capitalization labels for query {qi} in split {split_i} is not equal to number of "
            f"words. Number of words: {len(words)}, number of capitalization labels: "
            f"{len(capitalization_labels)}. Query: '{query}', "
            f"capitalization labels: '{capitalization_labels}'"
        )


def show_prog(q, total_num_lines, descriptions, units):
    prog = [
        tqdm(total=tt, desc=dd, unit=uu, unit_scale=True, position=i) for i, (tt, dd, uu)
        in enumerate(zip(total_num_lines, descriptions, units))
    ]
    finished = [False] * len(q)
    while True:
        for i, qq in enumerate(q):
            to_add = 0
            try:
                v = qq.get()
                print("v:", v)
                while v != -1:
                    to_add += v
                    v = qq.get()
                to_add = -1
            except Empty:
                if to_add == 0:
                    continue
            if to_add < 0:
                if prog[i].n < total_num_lines[i]:
                    logging.warning(
                        f"Progress process terminated before all progress bar reached 100 %. prog.n={prog[i].n}, "
                        f"total_num_lines={total_num_lines[i]}"
                    )
                finished[i] = True
                prog[i].close()
            prog[i].n += to_add
            prog[i].update(0)
            if prog[i].n >= total_num_lines[i]:
                finished[i] = True
                prog[i].close()
        if all(finished):
            break
        sleep(0.1)


class Progress:
    def __init__(self, total: Union[int, List[int]], desc: Union[str, List[str]], unit: Union[str, List[str]]):
        if not isinstance(total, list):
            total = [total]
        if not isinstance(desc, list):
            desc = [desc]
        if not isinstance(unit, list):
            unit = [unit]
        num_processes = max([len(total), len(desc), len(unit)])
        for param in [total, desc, unit]:
            if len(param) not in [num_processes, 1]:
                raise ValueError(
                    f"If parameter of `Progress.__init__` method is a list, then it has to be the same length as other "
                    f"parameters which are lists"
                )
            if len(param) == 1:
                param *= num_processes
        manager = mp.Manager()
        self.progress_queues = [manager.Queue() for _ in range(num_processes)]
        self.progress_process = mp.Process(target=show_prog, args=(self.progress_queues, total, desc, unit))
        self.progress_process.start()

    def __enter__(self):
        return self.get_queue()

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.finish()

    def get_queue(self):
        return self.progress_queues

    def finish(self):
        for q in self.progress_queues:
            q.put(-1)
        self.progress_process.join()


class TokenizeCreateMasksClipWorker:
    def __init__(
        self,
        max_seq_length,
        tokenizer,
        punct_label_ids,
        capit_label_ids,
        pad_label,
        with_label,
        verbose,
        progress_queue,
    ):
        self.max_seq_length = max_seq_length
        self.tokenizer = tokenizer
        self.punct_label_ids = punct_label_ids
        self.capit_label_ids = capit_label_ids
        self.pad_label = pad_label
        self.with_label = with_label
        self.verbose = verbose
        self.progress_queue = progress_queue

    def maybe_clip(self, values, prepend_value):
        if len(values) > self.max_seq_length:
            return [prepend_value] + values[-self.max_seq_length + 1:]
        return values

    def __call__(self, queries, punct_labels_lines, capit_labels_lines, split_i):
        all_input_ids, all_subtokens_mask, sent_lengths = [], [], []
        punct_all_labels, capit_all_labels = [], []
        progress_made = 0
        for i, query in enumerate(queries):
            words = query.strip().split()
            input_ids, subtokens_mask = [self.tokenizer.cls_id], [0]
            if self.with_label:
                check_number_of_labels(words, query, i, split_i, punct_labels_lines[i], capit_labels_lines[i])
                pad_id = self.punct_label_ids[self.pad_label]
                punct_labels = [pad_id]
                punct_query_labels = [self.punct_label_ids[lab] for lab in punct_labels_lines[i]]
                capit_labels = [pad_id]
                capit_query_labels = [self.capit_label_ids[lab] for lab in capit_labels_lines[i]]
            for j, word in enumerate(words):
                word_ids = self.tokenizer.text_to_ids(word)
                input_ids.extend(word_ids)

                subtokens_mask.append(1)
                subtokens_mask.extend([0] * (len(word_ids) - 1))

                if self.with_label:
                    punct_labels.extend([punct_query_labels[j]] * len(word_ids))
                    capit_labels.extend([capit_query_labels[j]] * len(word_ids))

            # add eos token
            input_ids.append(self.tokenizer.sep_id)
            subtokens_mask.append(0)
            sent_lengths.append(len(input_ids))

            all_input_ids.append(np.array(self.maybe_clip(input_ids, self.tokenizer.cls_id), dtype=np.int32))
            all_subtokens_mask.append(np.array(self.maybe_clip(subtokens_mask, 0), dtype=bool))

            if self.with_label:
                punct_labels.append(pad_id)
                punct_all_labels.append(np.array(self.maybe_clip(punct_labels, pad_id), dtype=np.int32))
                capit_labels.append(pad_id)
                capit_all_labels.append(np.array(self.maybe_clip(capit_labels, pad_id), dtype=np.int32))
            progress_made += 1
            if progress_made >= TOKENIZATION_PROGRESS_REPORT_PERIOD:
                self.progress_queue.put(progress_made)
                progress_made = 0
        if self.verbose:
            logging.info(f"Finished tokenization processing split with number {split_i}")
        return all_input_ids, all_subtokens_mask, sent_lengths, punct_all_labels, capit_all_labels


def tokenize_create_masks_clip_parallel(
    queries,
    max_seq_length,
    tokenizer,
    punct_label_ids,
    capit_label_ids,
    punct_labels_lines,
    capit_labels_lines,
    pad_label,
    with_label,
    verbose,
    njobs,
    progress_queue,
):
    create_progress_process = progress_queue is None
    if njobs is None:
        njobs = mp.cpu_count()
    if verbose:
        logging.info(f"Running tokenization with {njobs} jobs.")

    # Number of queries in split
    ss = min(len(queries) // max(njobs, 1), MAX_NUM_QUERIES_IN_SPLIT)
    n_split = len(queries) // ss
    split_queries = ([queries[ss * i : ss * (i + 1)] for i in range(n_split - 1)] + [queries[ss * (n_split - 1) :]])
    split_punct_labels_lines = (
        [punct_labels_lines[ss * i : ss * (i + 1)] for i in range(n_split - 1)]
        + [punct_labels_lines[ss * (n_split - 1) :]]
    )
    split_capit_labels_lines = (
        [capit_labels_lines[ss * i: ss * (i + 1)] for i in range(n_split - 1)]
        + [capit_labels_lines[ss * (n_split - 1):]]
    )
    args = list(zip(split_queries, split_punct_labels_lines, split_capit_labels_lines, range(n_split)))
    if create_progress_process:
        progress = Progress(len(queries), "Tokenization", "query")
        progress_queue = progress.get_queue()
    if njobs > 0:
        with mp.Pool(njobs) as pool:
            result = pool.starmap(
                TokenizeCreateMasksClipWorker(
                    max_seq_length,
                    tokenizer,
                    punct_label_ids,
                    capit_label_ids,
                    pad_label,
                    with_label,
                    verbose,
                    progress_queue,
                ),
                args,
            )
    else:
        result = []
        for x in args:
            result.append(
                TokenizeCreateMasksClipWorker(
                    max_seq_length,
                    tokenizer,
                    punct_label_ids,
                    capit_label_ids,
                    pad_label,
                    with_label,
                    verbose,
                    progress_queue,
                )(*x)
            )
    if create_progress_process:
        progress.finish()
    return tuple(list(itertools.chain(*e)) for e in zip(*result))


def get_features(
    queries: List[str],
    max_seq_length: int,
    tokenizer: TokenizerSpec,
    punct_label_ids: dict = None,
    capit_label_ids: dict = None,
    pad_label: str = 'O',
    punct_labels_lines=None,
    capit_labels_lines=None,
    verbose: bool = True,
    njobs: Optional[int] = None,
    progress_queue: Optional[mp.Queue] = None,
):
    """
    Processes the data and returns features.

    Args:
        queries: text sequences
        max_seq_length: max sequence length minus 2 for [CLS] and [SEP]
        tokenizer: such as AutoTokenizer
        pad_label: pad value use for labels. By default, it's the neutral label.
        punct_label_ids: dict to map punctuation labels to label ids.
            Starts with pad_label->0 and then increases in alphabetical order.
            Required for training and evaluation, not needed for inference.
        capit_label_ids: dict to map labels to label ids. Starts
            with pad_label->0 and then increases in alphabetical order.
            Required for training and evaluation, not needed for inference.
        punct_labels: list of labels for every word in a sequence (str)
        capit_labels: list of labels for every word in a sequence (str)

    Returns:
        all_input_ids: input ids for all tokens
        all_segment_ids: token type ids
        all_input_mask: attention mask to use for BERT model
        all_subtokens_mask: masks out all subwords besides the first one
        all_loss_mask: loss mask to mask out tokens during training
        punct_all_labels: all labels for punctuation task (ints)
        capit_all_labels: all labels for capitalization task (ints)
        punct_label_ids: label (str) to id (int) map for punctuation task
        capit_label_ids: label (str) to id (int) map for capitalization task
    """
    with_label = punct_labels_lines and capit_labels_lines
    if verbose:
        logging.info("Start initial tokenization.")
    input_ids, subtokens_mask, sent_lengths, punct_labels, capit_labels = tokenize_create_masks_clip_parallel(
        queries,
        max_seq_length,
        tokenizer,
        punct_label_ids,
        capit_label_ids,
        punct_labels_lines,
        capit_labels_lines,
        pad_label,
        with_label,
        verbose,
        njobs,
        progress_queue,
    )
    if verbose:
        logging.info("Finished initial tokenization.")
        get_stats(sent_lengths)
        logging.info(f"Finished clipping and padding.")
        for i in range(min(len(input_ids), 5)):
            logging.info("*** Example ***")
            logging.info("i: %s" % (i))
            logging.info("subtokens: %s" % " ".join(list(map(str, input_ids[i]))))
            logging.info("subtokens_mask: %s" % " ".join(list(map(str, subtokens_mask[i]))))
            if with_label:
                logging.info("punct_labels: %s" % " ".join(list(map(str, punct_labels[i]))))
                logging.info("capit_labels: %s" % " ".join(list(map(str, capit_labels[i]))))
    return input_ids, subtokens_mask, punct_labels, capit_labels


def get_masks_and_segment_ids(input_ids, subtokens_mask, pad_id, cls_id, sep_id, ignore_start_end, ignore_extra_tokens):
    segment_ids = np.zeros_like(input_ids, dtype=np.int8)
    input_mask = np.not_equal(input_ids, pad_id)
    special_mask = np.equal(input_ids, cls_id) & np.equal(input_ids, sep_id)
    if ignore_start_end:
        if ignore_extra_tokens:
            loss_mask = subtokens_mask
        else:
            loss_mask = input_mask & ~special_mask
    else:
        if ignore_extra_tokens:
            loss_mask = subtokens_mask | special_mask
        else:
            loss_mask = input_mask
    return segment_ids, input_mask, loss_mask


def create_label_ids(unique_labels, pad_label):
    label_ids = {pad_label: 0}
    if pad_label in unique_labels:
        unique_labels.remove(pad_label)
    for label in sorted(unique_labels):
        label_ids[label] = len(label_ids)
    return label_ids


class BertPunctuationCapitalizationDataset(Dataset):
    """
    Creates dataset to use during training for punctuaion and capitalization tasks with a pretrained model.
    For dataset to use during inference without labels, see BertPunctuationCapitalizationInferDataset.

    Args:
        text_file: file to sequences, each line should a sentence, no header.
        label_file: file to labels, each line corresponds to word labels for a sentence in the text_file. No header.
        max_seq_length: max sequence length minus 2 for [CLS] and [SEP]
        tokenizer: such as AutoTokenizer
        num_samples: number of samples you want to use for the dataset.
            If -1, use all dataset. Useful for testing.
        pad_label: pad value use for labels.
            by default, it's the neutral label.
        punct_label_ids and capit_label_ids (dict):
            dict to map labels to label ids.
            Starts with pad_label->0 and then increases in alphabetical order
            For dev set use label_ids generated during training to support
            cases when not all labels are present in the dev set.
            For training set label_ids should be None or loaded from cache
        ignore_extra_tokens: whether to ignore extra tokens in the loss_mask
        ignore_start_end: whether to ignore bos and eos tokens in the loss_mask
        use_cache: whether to use processed data cache or not
        get_label_frequencies: whether to generate label frequencies
        punct_label_ids_file and capit_label_ids_file: name of the files to save in .nemo
    """

    @property
    def output_types(self) -> Optional[Dict[str, NeuralType]]:
        """Returns definitions of module output ports. """
        return {
            'input_ids': NeuralType(('B', 'T'), ChannelType()),
            'segment_ids': NeuralType(('B', 'T'), ChannelType()),
            'input_mask': NeuralType(('B', 'T'), MaskType()),
            'subtokens_mask': NeuralType(('B', 'T'), MaskType()),
            'loss_mask': NeuralType(('B', 'T'), MaskType()),
            'punct_labels': NeuralType(('B', 'T'), LabelsType()),
            'capit_labels': NeuralType(('B', 'T'), LabelsType()),
        }

    def __init__(
        self,
        text_file: str,
        label_file: str,
        max_seq_length: int,
        tokenizer: TokenizerSpec,
        num_samples: int = -1,
        tokens_in_batch: int = 1024,
        pad_label: str = 'O',
        punct_label_ids: Dict[str, int] = None,
        capit_label_ids: Dict[str, int] = None,
        ignore_extra_tokens: bool = False,
        ignore_start_end: bool = False,
        use_cache: bool = True,
        get_label_frequencies: bool = False,
        punct_label_ids_file: str = 'punct_label_ids.csv',
        capit_label_ids_file: str = 'capit_label_ids.csv',
        add_masks_and_segment_ids_to_batch: bool = True,
        verbose: bool = True,
        pickle_features: bool = True,
        njobs: Optional[int] = None,
        tokenization_progress_queue: Optional[mp.Queue] = None,
        batch_mark_up_progress_queue: Optional[mp.Queue] = None,
        batch_building_progress_queue: Optional[mp.Queue] = None,
    ):
        """ Initializes BertPunctuationCapitalizationDataset. """

        if not (os.path.exists(text_file) and os.path.exists(label_file)):
            raise FileNotFoundError(
                f'{text_file} or {label_file} not found. The data should be splitted into 2 files: text.txt and \
                labels.txt. Each line of the text.txt file contains text sequences, where words are separated with \
                spaces. The labels.txt file contains corresponding labels for each word in text.txt, the labels are \
                separated with spaces. Each line of the files should follow the format:  \
                   [WORD] [SPACE] [WORD] [SPACE] [WORD] (for text.txt) and \
                   [LABEL] [SPACE] [LABEL] [SPACE] [LABEL] (for labels.txt).'
            )

        # Cache features
        data_dir = os.path.dirname(text_file)
        filename = os.path.basename(text_file)

        if not filename.endswith('.txt'):
            raise ValueError("{text_file} should have extension .txt")

        self.tokens_in_batch = tokens_in_batch
        self.tokenizer = tokenizer
        self.pad_label = pad_label
        self.ignore_extra_tokens = ignore_extra_tokens
        self.ignore_start_end = ignore_start_end
        self.add_masks_and_segment_ids_to_batch = add_masks_and_segment_ids_to_batch
        self.verbose = verbose
        self.batch_mark_up_progress_queue = batch_mark_up_progress_queue
        self.batch_building_progress_queue = batch_building_progress_queue
        filename = filename[:-4]
        vocab_size = getattr(self.tokenizer, "vocab_size", 0)
        features_pkl = os.path.join(
            data_dir,
            "cached_{}_{}_{}_{}_{}".format(
                filename, self.tokenizer.name, str(max_seq_length), str(vocab_size), str(num_samples)
            ),
        )

        self.punct_label_ids_file = os.path.join(data_dir, punct_label_ids_file)
        self.capit_label_ids_file = os.path.join(data_dir, capit_label_ids_file)

        master_device = not torch.distributed.is_initialized() or torch.distributed.get_rank() == 0
        cache_files_exist = (
            os.path.exists(features_pkl)
            and os.path.exists(self.punct_label_ids_file)
            and os.path.exists(self.capit_label_ids_file)
        )
        features = None
        if master_device and not (cache_files_exist and use_cache):
            if num_samples == 0:
                raise ValueError("num_samples has to be positive", num_samples)
            if verbose:
                logging.info(f'Processing {text_file}')
            with open(text_file, 'r') as f:
                text_lines = f.readlines()

            # Collect all possible labels
            punct_unique_labels = set()
            capit_unique_labels = set()
            punct_labels_lines = []
            capit_labels_lines = []
            with open(label_file, 'r') as f:
                for line in f:
                    line = line.strip().split()

                    # extract punctuation and capitalization labels
                    punct_line, capit_line = zip(*line)
                    punct_labels_lines.append(punct_line)
                    capit_labels_lines.append(capit_line)

                    punct_unique_labels.update(punct_line)
                    capit_unique_labels.update(capit_line)

            if len(punct_labels_lines) != len(text_lines):
                raise ValueError("Labels file should contain labels for every word")

            dataset = list(zip(text_lines, punct_labels_lines, capit_labels_lines))

            if num_samples > 0:
                dataset = dataset[:num_samples]

            dataset = list(zip(*dataset))
            text_lines = dataset[0]
            punct_labels_lines = dataset[1]
            capit_labels_lines = dataset[2]

            # for dev/test sets use label mapping from training set
            if punct_label_ids:
                if self.verbose:
                    if len(punct_label_ids) != len(punct_unique_labels):
                        logging.info(
                            'Not all labels from the specified'
                            + 'label_ids dictionary are present in the'
                            + 'current dataset. Using the provided'
                            + 'label_ids dictionary.'
                        )
                    else:
                        logging.info('Using the provided label_ids dictionary.')
            else:
                if self.verbose:
                    logging.info(
                        'Creating a new label to label_id dictionary.'
                        + ' It\'s recommended to use label_ids generated'
                        + ' during training for dev/test sets to avoid'
                        + ' errors if some labels are not'
                        + ' present in the dev/test sets.'
                        + ' For training set label_ids should be None.'
                    )

                punct_label_ids = create_label_ids(punct_unique_labels, self.pad_label)
                capit_label_ids = create_label_ids(capit_unique_labels, self.pad_label)

            self._save_label_ids(punct_label_ids, self.punct_label_ids_file)
            self._save_label_ids(capit_label_ids, self.capit_label_ids_file)

            features = get_features(
                text_lines,
                max_seq_length,
                self.tokenizer,
                pad_label=self.pad_label,
                punct_labels_lines=punct_labels_lines,
                capit_labels_lines=capit_labels_lines,
                punct_label_ids=punct_label_ids,
                capit_label_ids=capit_label_ids,
                verbose=self.verbose,
                progress_queue=tokenization_progress_queue,
                njobs=njobs,
            )
            if pickle_features:
                pickle.dump(tuple(list(features) + [punct_label_ids, capit_label_ids]), open(features_pkl, "wb"))
                if self.verbose:
                    logging.info(f'Features saved to {features_pkl}')

        # wait until the master process writes to the processed data files
        if torch.distributed.is_initialized():
            torch.distributed.barrier()

        if features is None:
            features = pickle.load(open(features_pkl, 'rb'))
            punct_label_ids, capit_label_ids = features[-2], features[-1]
            features = features[:-2]
            if tokenization_progress_queue is not None:
                tokenization_progress_queue.put(len(features[0]))
            if self.verbose:
                logging.info(f'Features restored from {features_pkl}')

        input_ids = features[0]
        subtokens_mask = features[1]
        punct_labels = features[2]
        capit_labels = features[3]
        self.punct_label_ids = punct_label_ids
        self.capit_label_ids = capit_label_ids
        self.batches = self.pack_into_batches(
            input_ids, subtokens_mask, punct_labels, capit_labels
        )

        if get_label_frequencies:
            self.punct_label_frequencies = self._calculate_label_frequencies(self.punct_all_labels, data_dir, 'punct')
            self.capit_label_frequencies = self._calculate_label_frequencies(self.capit_all_labels, data_dir, 'capit')

    def pad(self, vectors, length, value):
        result = []
        for v in vectors:
            result.append(np.concatenate([v, np.full([length - v.shape[0]], value, dtype=v.dtype)]))
        return np.stack(result)

    def mark_up_batches(self, input_ids):
        batch_beginnings, batch_sizes, batch_seq_lengths = [], [], []
        current_max_length = 0
        start = 0
        if self.batch_mark_up_progress_queue is None:
            inp_iterator = tqdm(enumerate(input_ids), total=len(input_ids), desc="Batch mark up", unit="query")
        else:
            inp_iterator = enumerate(input_ids)
            progress_made = 0
        for i, inp in inp_iterator:
            current_max_length = max(current_max_length, ceil(len(inp) / 8) * 8)
            if current_max_length * (i + 1 - start) > self.tokens_in_batch:
                batch_size = (i - start) // 8 * 8
                if batch_size == 0:
                    if i > start:
                        batch_size = i - start
                        logging.warning(
                            f"Could not create batch with multiple of 8 size. Probably there is a too long sequence in "
                            f"the dataset. current_max_length={current_max_length}. Batch size will be reduced to "
                            f"{batch_size}. tokens_in_batch={self.tokens_in_batch}. The batch includes sequences from "
                            f"{start} to {i - 1}.")
                    else:
                        logging.warning(
                            f"Input sequence number {i - 1} is too long. Could not fit it into batch with "
                            f"{self.tokens_in_batch} tokens. Sequence number {i - 1} will not be added to batches."
                        )
                        start = i
                        current_max_length = ceil(len(inp) / 8) * 8
                        continue
                seq_length = ceil(max([len(inp) for inp in input_ids[start : start + batch_size]]) / 8) * 8
                batch_beginnings.append(start)
                batch_sizes.append(batch_size)
                batch_seq_lengths.append(seq_length)
                start += batch_size
                current_max_length = ceil(max([len(inp) for inp in input_ids[start : i + 1]]) / 8) * 8
            if self.batch_mark_up_progress_queue is not None:
                progress_made += 1
                if progress_made >= BATCH_MARK_UP_PROGRESS_REPORT_PERIOD:
                    self.batch_mark_up_progress_queue.put(progress_made)
                    progress_made = 0
        if start < len(input_ids):
            seq_length = ceil(max([len(inp) for inp in input_ids[start :]]) / 8) * 8
            batch_beginnings.append(start)
            batch_sizes.append(len(input_ids) - start)
            batch_seq_lengths.append(seq_length)
            if self.batch_mark_up_progress_queue is not None:
                self.batch_mark_up_progress_queue.put(progress_made)
        assert sum(batch_sizes) == len(input_ids)
        for i in range(len(batch_beginnings) - 1):
            assert batch_beginnings[i] + batch_sizes[i] == batch_beginnings[i + 1]
            assert batch_seq_lengths[i] >= max(
                [len(inp) for inp in input_ids[batch_beginnings[i] : batch_beginnings[i] + batch_sizes[i]]]
            )
        return batch_beginnings, batch_sizes, batch_seq_lengths

    def pack_into_batches(
        self, input_ids, subtokens_mask, punct_labels, capit_labels
    ):
        zipped = sorted(zip(input_ids, subtokens_mask, punct_labels, capit_labels), key=lambda x: x[0].shape[0])
        input_ids, subtokens_mask, punct_labels, capit_labels = zip(*zipped)
        batch_beginnings, batch_sizes, batch_seq_lengths = self.mark_up_batches(input_ids)
        batches = []
        if self.batch_building_progress_queue is None:
            inp_iterator = tqdm(
                zip(batch_beginnings, batch_sizes, batch_seq_lengths),
                total=len(batch_beginnings),
                desc="Batch building",
                unit="batch",
            )
        else:
            # In this case we report number of queries not number of batches
            inp_iterator = zip(batch_beginnings, batch_sizes, batch_seq_lengths)
            progress_made = 0
        for start, size, length in inp_iterator:
            batch_input_ids = self.pad(input_ids[start : start + size], length, self.tokenizer.pad_id)
            batch_subtokens_mask = self.pad(subtokens_mask[start : start + size], length, False)
            batch = {
                "input_ids": batch_input_ids,
                "subtokens_mask": batch_subtokens_mask,
                "punct_labels": self.pad(
                    punct_labels[start : start + size], length, self.punct_label_ids[self.pad_label]
                ).astype(np.int64),
                "capit_labels": self.pad(
                    capit_labels[start : start + size], length, self.capit_label_ids[self.pad_label]
                ).astype(np.int64),
            }
            if self.add_masks_and_segment_ids_to_batch:
                batch_segment_ids, batch_input_mask, batch_loss_mask = get_masks_and_segment_ids(
                    batch_input_ids,
                    batch_subtokens_mask,
                    self.tokenizer.pad_id,
                    self.tokenizer.cls_id,
                    self.tokenizer.sep_id,
                    self.ignore_start_end,
                    self.ignore_extra_tokens,
                )
                batch['segment_ids'] = batch_segment_ids
                batch['input_mask'] = batch_input_mask
                batch['loss_mask'] = batch_loss_mask
            batches.append(batch)
            if self.batch_building_progress_queue is not None:
                progress_made += size
                if progress_made >= BATCH_BUILDING_PROGRESS_REPORT_PERIOD:
                    self.batch_building_progress_queue.put(progress_made)
                    progress_made = 0
        if self.batch_building_progress_queue is not None:
            self.batch_building_progress_queue.put(progress_made)
        random.shuffle(batches)
        return batches

    def _calculate_label_frequencies(self, all_labels: List[int], data_dir: str, name: str) -> Dict[str, float]:
        """ Calculates labels frequencies """
        merged_labels = itertools.chain.from_iterable(all_labels)
        if self.verbose:
            logging.info('Three most popular labels')
        _, label_frequencies, _ = get_label_stats(merged_labels, data_dir + '/label_count_' + name + '.tsv')
        return label_frequencies

    def _save_label_ids(self, label_ids: Dict[str, int], filename: str) -> None:
        """ Saves label ids map to a file """
        with open(filename, 'w') as out:
            labels, _ = zip(*sorted(label_ids.items(), key=lambda x: x[1]))
            out.write('\n'.join(labels))
            if self.verbose:
                logging.info(f'Labels: {label_ids}')
                logging.info(f'Labels mapping saved to : {out.name}')

    def __len__(self):
        return len(self.batches)

    def collate_fn(self, batch):
        return {k: torch.as_tensor(v) for k, v in batch[0].items()}

    def __getitem__(self, idx):
        return self.batches[idx]


class BertPunctuationCapitalizationTarredDataset(IterableDataset):
    def __init__(
        self,
        metadata_file: os.PathLike,
        ignore_extra_tokens: bool = False,
        ignore_start_end: bool = False,

    ):
        metadata_file = Path(metadata_file).expanduser()
        with open(metadata_file) as f:
            self.metadata = json.load(f)
        self.ignore_extra_tokens = ignore_extra_tokens
        self.ignore_start_end = ignore_start_end
        self.tar_files = []
        for file_path in self.metadata['tar_files']:
            file_path = Path(file_path).expanduser()
            if file_path.is_absolute():
                self.tar_files.append(file_path)
            else:
                self.tar_files.append(metadata_file.parent / file_path)



def _get_subtokens_and_subtokens_mask(query: str, tokenizer: TokenizerSpec) -> Tuple[List[str], List[int]]:
    """
    Tokenizes input query into subtokens and creates subtokens mask. Subtokens mask is an array of the same length as
    subtokens array and contains zeros and ones in which. If element of mask equals 1, then corresponding subtoken in
    subtokens array is first subtoken in some word
    Args:
        query: a string that will be tokenized
        tokenizer: an instance of tokenizer
    Returns:
        subtokens: list of subtokens
        subtokens_mask: list of ints
    """
    words = query.strip().split()
    subtokens = []
    subtokens_mask = []
    for j, word in enumerate(words):
        word_tokens = tokenizer.text_to_tokens(word)
        subtokens.extend(word_tokens)
        subtokens_mask.append(1)
        subtokens_mask.extend([0] * (len(word_tokens) - 1))
    return subtokens, subtokens_mask


def _check_max_seq_length_and_margin_and_step(max_seq_length: int, margin: int, step: int):
    """
    Checks values of ``max_seq_length``, ``margin``, and ``step``.
    Args:
        max_seq_length: a segment length with ``[CLS]`` and ``[SEP]`` tokens
        margin: a number of input tokens near edges of segments which are not used in punctuation and capitalization
            prediction.
        step: offset of consequent segments.
    Returns:
        None
    """
    if max_seq_length < 3:
        raise ValueError(
            f"Parameter `max_seq_length={max_seq_length}` cannot be less than 3 because `max_seq_length` is a length "
            f"of a segment with [CLS] and [SEP] tokens."
        )
    if margin >= (max_seq_length - 2) // 2 and margin > 0 or margin < 0:
        raise ValueError(
            f"Parameter `margin` has to be not negative and less than `(max_seq_length - 2) // 2`. Don't forget about "
            f"CLS and EOS tokens in the beginning and the end of segment. margin={margin}, "
            f"max_seq_length={max_seq_length}"
        )
    if step <= 0:
        raise ValueError(f"Parameter `step` has to be positive whereas step={step}")
    if step > max_seq_length - 2 - 2 * margin:
        logging.warning(
            f"Parameter step={step} is too big. It will be reduced to `min(max_seq_length, <maximum query length> + 2) "
            f"- 2 - 2 * margin`."
        )


def get_features_infer(
    queries: List[str],
    tokenizer: TokenizerSpec,
    max_seq_length: int = 64,
    step: Optional[int] = 8,
    margin: Optional[int] = 16,
) -> Tuple[
    List[List[int]], List[List[int]], List[List[int]], List[List[int]], List[int], List[int], List[bool], List[bool],
]:
    """
    Processes the data and returns features.

    Args:
        queries: text sequences
        tokenizer: such as AutoTokenizer
        max_seq_length: max sequence length minus 2 for [CLS] and [SEP]
        step: relative shift of consequent segments into which long queries are split. Long queries are split into
            segments which can overlap. Parameter ``step`` controls such overlapping. Imagine that queries are
            tokenized into characters, ``max_seq_length=5``, and ``step=2``. In such a case query "hello" is
            tokenized into segments ``[['[CLS]', 'h', 'e', 'l', '[SEP]'], ['[CLS]', 'l', 'l', 'o', '[SEP]']]``.
        margin: number of subtokens near edges of segments which are not used for punctuation and capitalization
            prediction. The first segment does not have left margin and the last segment does not have right
            margin. For example, if input sequence is tokenized into characters, ``max_seq_length=5``,
            ``step=1``, and ``margin=1``, then query "hello" will be tokenized into segments
            ``[['[CLS]', 'h', 'e', 'l', '[SEP]'], ['[CLS]', 'e', 'l', 'l', '[SEP]'],
            ['[CLS]', 'l', 'l', 'o', '[SEP]']]``. These segments are passed to the model. Before final predictions
            computation, margins are removed. In the next list, subtokens which logits are not used for final
            predictions computation are marked with asterisk: ``[['[CLS]'*, 'h', 'e', 'l'*, '[SEP]'*],
            ['[CLS]'*, 'e'*, 'l', 'l'*, '[SEP]'*], ['[CLS]'*, 'l'*, 'l', 'o', '[SEP]'*]]``.

    Returns:
        all_input_ids: list of input ids of all segments
        all_segment_ids: token type ids of all segments
        all_input_mask: attention mask to use for BERT model
        all_subtokens_mask: masks out all subwords besides the first one
        all_quantities_of_preceding_words: number of words in query preceding a segment. Used for joining
            predictions from overlapping segments.
        all_query_ids: index of a query to which segment belongs
        all_is_first: is segment first segment in a query
        all_is_last: is segment last segment in a query
    """
    st = []
    stm = []
    sent_lengths = []
    for i, query in enumerate(queries):
        subtokens, subtokens_mask = _get_subtokens_and_subtokens_mask(query, tokenizer)
        sent_lengths.append(len(subtokens))
        st.append(subtokens)
        stm.append(subtokens_mask)
    _check_max_seq_length_and_margin_and_step(max_seq_length, margin, step)
    max_seq_length = min(max_seq_length, max(sent_lengths) + 2)
    logging.info(f'Max length: {max_seq_length}')
    # Maximum number of word subtokens in segment. The first and the last tokens in segment are CLS and EOS
    length = max_seq_length - 2
    step = min(length - margin * 2, step)
    get_stats(sent_lengths)
    all_input_ids, all_segment_ids, all_subtokens_mask, all_input_mask, all_input_mask = [], [], [], [], []
    all_quantities_of_preceding_words, all_query_ids, all_is_first, all_is_last = [], [], [], []
    for q_i, query_st in enumerate(st):
        q_inp_ids, q_segment_ids, q_subtokens_mask, q_inp_mask, q_quantities_of_preceding_words = [], [], [], [], []
        for i in range(0, max(len(query_st), length) - length + step, step):
            subtokens = [tokenizer.cls_token] + query_st[i : i + length] + [tokenizer.sep_token]
            q_inp_ids.append(tokenizer.tokens_to_ids(subtokens))
            q_segment_ids.append([0] * len(subtokens))
            q_subtokens_mask.append([0] + stm[q_i][i : i + length] + [0])
            q_inp_mask.append([1] * len(subtokens))
            q_quantities_of_preceding_words.append(np.count_nonzero(stm[q_i][:i]))
        all_input_ids.append(q_inp_ids)
        all_segment_ids.append(q_segment_ids)
        all_subtokens_mask.append(q_subtokens_mask)
        all_input_mask.append(q_inp_mask)
        all_quantities_of_preceding_words.append(q_quantities_of_preceding_words)
        all_query_ids.append([q_i] * len(q_inp_ids))
        all_is_first.append([True] + [False] * (len(q_inp_ids) - 1))
        all_is_last.append([False] * (len(q_inp_ids) - 1) + [True])
    return (
        list(itertools.chain(*all_input_ids)),
        list(itertools.chain(*all_segment_ids)),
        list(itertools.chain(*all_input_mask)),
        list(itertools.chain(*all_subtokens_mask)),
        list(itertools.chain(*all_quantities_of_preceding_words)),
        list(itertools.chain(*all_query_ids)),
        list(itertools.chain(*all_is_first)),
        list(itertools.chain(*all_is_last)),
    )


class BertPunctuationCapitalizationInferDataset(Dataset):
    """
    Creates dataset to use during inference for punctuation and capitalization tasks with a pretrained model.
    For dataset to use during training with labels, see BertPunctuationCapitalizationDataset.

    Parameters ``max_seq_length``, ``step``, ``margin`` are for controlling the way queries are split into segments
    which then processed by the model. Parameter ``max_seq_length`` is a length of a segment after tokenization
    including special tokens [CLS] in the beginning and [SEP] in the end of a segment. Parameter ``step`` is shift
    between consequent segments. Parameter ``margin`` is used to exclude negative effect of subtokens near
    borders of segments which have only one side context.

    Args:
        queries: list of sequences.
        tokenizer: such as AutoTokenizer
        max_seq_length: max sequence length minus 2 for [CLS] and [SEP]
        step: relative shift of consequent segments into which long queries are split. Long queries are split into
            segments which can overlap. Parameter ``step`` controls such overlapping. Imagine that queries are
            tokenized into characters, ``max_seq_length=5``, and ``step=2``. In such a case query "hello" is
            tokenized into segments ``[['[CLS]', 'h', 'e', 'l', '[SEP]'], ['[CLS]', 'l', 'l', 'o', '[SEP]']]``.
        margin: number of subtokens in the beginning and the end of segments which are not used for prediction
            computation. The first segment does not have left margin and the last segment does not have right
            margin. For example, if input sequence is tokenized into characters, ``max_seq_length=5``,
            ``step=1``, and ``margin=1``, then query "hello" will be tokenized into segments
            ``[['[CLS]', 'h', 'e', 'l', '[SEP]'], ['[CLS]', 'e', 'l', 'l', '[SEP]'],
            ['[CLS]', 'l', 'l', 'o', '[SEP]']]``. These segments are passed to the model. Before final predictions
            computation, margins are removed. In the next list, subtokens which logits are not used for final
            predictions computation are marked with asterisk: ``[['[CLS]'*, 'h', 'e', 'l'*, '[SEP]'*],
            ['[CLS]'*, 'e'*, 'l', 'l'*, '[SEP]'*], ['[CLS]'*, 'l'*, 'l', 'o', '[SEP]'*]]``.
    """

    @property
    def output_types(self) -> Optional[Dict[str, NeuralType]]:
        """Returns definitions of module output ports.

        input_ids: ids of word subtokens encoded using tokenizer
        segment_ids: an array of zeros
        input_mask: attention mask. Zeros if input is padding.
        subtoken_mask: a mask used for retrieving predictions for words. An element equals ``1`` if corresponding
            token is the first token in some word and zero otherwise. For example, if input query
            "language processing" is tokenized into ["[CLS]", "language", "process", "ing", "SEP"], then
            ``subtokens_mask`` will be [0, 1, 1, 0, 0].
        quantities_of_preceding_words: number of words preceding a segment in a query. It is used for uniting
            predictions from different segments if such segments overlap. For example, if query "hello john" is
            tokenized into segments ``[['hell', 'o'], ['john']]``, then ``quantities_of_preceding_words=[0, 1]``.
        query_ids: ids of queries to which segments belong. For example, if ``queries=["foo", "bar"]`` are
            segmented into ``[[['[CLS]', 'f', 'o', '[SEP]'], ['[CLS]', 'o', 'o', '[SEP]']],
            [['[CLS]', 'b', 'a', '[SEP]'], ['[CLS]', 'a', 'r', '[SEP]']]]``, then for batch
            [['[CLS]', 'o', 'o', '[SEP]'], ['[CLS]', 'b', 'a', '[SEP]'], ['[CLS]', 'a', 'r', '[SEP]']]
            ``query_ids=[0, 1, 1]``.
        is_first: is segment the first segment in query. The left margin of the first segment in a query is not
            removed and this parameter is used to identify first segments.
        is_last: is segment the last segment in query. The right margin of the last segment in a query is not
            removed and this parameter is used to identify last segments.

        """
        return {
            'input_ids': NeuralType(('B', 'T'), ChannelType()),
            'segment_ids': NeuralType(('B', 'T'), ChannelType()),
            'input_mask': NeuralType(('B', 'T'), MaskType()),
            'subtokens_mask': NeuralType(('B', 'T'), MaskType()),
            'quantities_of_preceding_words': NeuralType(('B',), Index()),
            'query_ids': NeuralType(('B',), Index()),
            'is_first': NeuralType(('B',), BoolType()),
            'is_last': NeuralType(('B',), BoolType()),
        }

    def __init__(
        self, queries: List[str], tokenizer: TokenizerSpec, max_seq_length: int = 128, step: int = 32, margin: int = 16
    ):
        features = get_features_infer(
            queries=queries, max_seq_length=max_seq_length, tokenizer=tokenizer, step=step, margin=margin
        )
        self.all_input_ids: List[List[int]] = features[0]
        self.all_segment_ids: List[List[int]] = features[1]
        self.all_input_mask: List[List[int]] = features[2]
        self.all_subtokens_mask: List[List[int]] = features[3]
        self.all_quantities_of_preceding_words: List[int] = features[4]
        self.all_query_ids: List[int] = features[5]
        self.all_is_first: List[bool] = features[6]
        self.all_is_last: List[bool] = features[7]

    def __len__(self) -> int:
        return len(self.all_input_ids)

    def collate_fn(
        self, batch: List[Tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray, int, int, bool, bool]]
    ) -> Tuple[
        torch.Tensor, torch.Tensor, torch.Tensor, torch.Tensor, Tuple[int], Tuple[int], Tuple[bool], Tuple[bool]
    ]:
        inp_ids, segment_ids, inp_mask, st_mask, n_preceding, query_ids, is_first, is_last = zip(*batch)
        return (
            pad_sequence([torch.tensor(x) for x in inp_ids], batch_first=True, padding_value=0),
            pad_sequence([torch.tensor(x) for x in segment_ids], batch_first=True, padding_value=0),
            pad_sequence([torch.tensor(x) for x in inp_mask], batch_first=True, padding_value=0),
            pad_sequence([torch.tensor(x) for x in st_mask], batch_first=True, padding_value=0),
            n_preceding,
            query_ids,
            is_first,
            is_last,
        )

    def __getitem__(self, idx: int) -> Tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray, int, int, bool, bool]:
        return (
            np.array(self.all_input_ids[idx]),
            np.array(self.all_segment_ids[idx]),
            np.array(self.all_input_mask[idx], dtype=np.float32),
            np.array(self.all_subtokens_mask[idx]),
            self.all_quantities_of_preceding_words[idx],
            self.all_query_ids[idx],
            self.all_is_first[idx],
            self.all_is_last[idx],
        )
