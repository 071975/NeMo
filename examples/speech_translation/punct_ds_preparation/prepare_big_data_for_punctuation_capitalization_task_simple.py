import argparse
import copy
import html
import itertools
import logging
import multiprocessing as mp
import os
import random
import re
from itertools import accumulate, chain
from pathlib import Path
from subprocess import PIPE, Popen, run
from tempfile import TemporaryDirectory
from typing import Dict, List, Optional, Set, Tuple, Union

import chardet
import nltk
import numpy as np
from bs4 import BeautifulSoup, NavigableString
from tqdm import tqdm

from nemo.collections.common.tokenizers import TokenizerSpec
from nemo.collections.nlp.data.token_classification.punctuation_capitalization_dataset import Progress
from nemo.collections.nlp.modules import get_tokenizer

import prepare_big_data_for_punctuation_capitalization_task_complex as big
import prepare_small_data_for_punctuation_capitalization_task as small
from prepare_small_data_for_punctuation_capitalization_task import WC

logging.basicConfig(level="INFO", format='%(levelname)s -%(asctime)s - %(name)s - %(message)s')

random.seed(42)


SUPPORTED_CORPUS_TYPES = [
    "wikipedia",
    "europarl",
    "TED",
    "rapid",
    "news-commentary",
    "wiki-extracted",
    "news-crawl",
    "pg19",
    "pubmed",
    "google-normalization-dataset",
    "tatoeba",
    "europarl-raw",
    "un",
]


FORBIDDEN_PUNCTUATION_IN_THE_START_OF_SEGMENT = re.compile(f'^[^{WC}]+')

WIKI_EXTRACTED_NOT_EMPTY_DOC = re.compile('^<doc id="[^\n]+\n[^\n]+\n+[^\n]+')
WIKI_EXTRACTED_HEADER = re.compile(r'^<doc id="([^"]+)" url="([^"]+)" title="([^"]+)">$', flags=re.MULTILINE)
WIKI_EXTRACTED_DOC_PROGRESS_PERIOD = 100

SEVERAL_NEW_LINES_PATTERN = re.compile('(?:\n[ \t]*){2,}')
LIST_PATTERN = re.compile(f'^ *(?:{small.ROMAN_NUMERAL.pattern}|[0-9]+|[a-z]) *[.)]', flags=re.I | re.MULTILINE)
NEW_LINE_WITH_SPACES_PATTERN = re.compile(' *\n *')
DOUBLE_HYPHEN_PATTERN = re.compile(' *-- *')
SQUARE_BRACKETS_PATTERN = re.compile(r' ?\[[^]]+] *')
UNDERSCORE_PATTERN = re.compile(fr'(?<![{WC}/])_([^_]+)_(?![{WC}/])')
WORD_CHAR_ENDING_PATTERN = re.compile(f'[{WC}]$')
UPPERCASE_INTRO = re.compile('[A-Z ]{2,}: ([A-Z])')
SHORT_LINE = re.compile('^.{1,50}\n')
LETTER = re.compile('[a-zA-Z]')

NUM_LINES_PER_NEWS_CRAWL_TMP_FILE = 10 ** 6

MAX_NUM_CHARACTERS_IN_1_FILE = 10 ** 9
BUFFER_SIZE = 2 ** 24
REPORT_PROGRESS_PERIOD = 5000

INTACT_SENTENCES_PROGRESS_PERIOD = 10000

PG_19_MIN_PARAGRAPH_LEN = 100

MAX_FRACTION_OF_WORDS_WITHOUT_LETTERS = 0.7
MAX_QUOTIENT_OF_NUMBER_OF_DOTS_IN_SENTENCE = 0.5
MIN_NUM_WORDS_FOR_FRACTION_CRITERIA = 15

GOOGLE_NORMALIZATION_DATASET_MIN_NUM_WORDS_IN_SENTENCE = 6

EUROPARL_RAW_REPORTED_SPEECH = re.compile("^[^.\n]+\\. - (?:\\([^)]+\\) )?", flags=re.MULTILINE)
EUROPARL_RAW_LANG_DISCLAIMER = re.compile("^\\([^)]+\\) ?", flags=re.MULTILINE)
EUROPARL_RAW_SPEAKER_LINE = re.compile("^<SPEAKER [^>\n]+> *\n", flags=re.MULTILINE)
EUROPARL_RAW_CHAPTER = re.compile("^<CHAPTER[^>\n]+> *\n[^<]*", flags=re.MULTILINE)

UN_PARAGRAPH_START = re.compile("<p(?: id=\"[1-9][0-9]*\")?>")
UN_SENTENCE_START = re.compile("<s(?: id=\"[0-9:]+\")?(?: lang=\"[a-zA-Z]+\")?>")

ENUMERATION_START = re.compile(r'^[0-9]+\. *|^[0-9]', flags=re.MULTILINE)
ALPHA_ENUMERATION_START = re.compile(r'^\([a-z]+\) *', flags=re.MULTILINE)
BULLET_START = re.compile('^- *', flags=re.MULTILINE)
ROMAN_ENUMERATION_START = re.compile(r'[A-Za-z]+. *', flags=re.MULTILINE)
UN_FORBIDDEN_ENUMERATION_START = re.compile(
    '|'.join([ALPHA_ENUMERATION_START.pattern, BULLET_START.pattern, ROMAN_ENUMERATION_START.pattern])
)


def count_in_blocks(files, size=BUFFER_SIZE, specific_to_count=None, num_characters=None):
    total_num_characters = 0
    finished = False
    while True:
        b = files.read(size)
        if not b:
            break
        if num_characters is not None:
            if total_num_characters + len(b) >= num_characters:
                b = b[:num_characters - total_num_characters]
                finished = True
        if specific_to_count is None:
            yield len(b)
        else:
            yield b.count(specific_to_count)
        if finished:
            break


def count_lines_in_file(file_path: Path, start: int = 0, num_characters: Optional[int] = None) -> int:
    with file_path.open() as f:
        f.seek(start)
        count = sum(count_in_blocks(f, specific_to_count='\n', num_characters=num_characters))
    return count


def count_lines_in_file_fast(file_path: Path) -> int:
    result = run(['wc', '-l', str(file_path)], stdout=PIPE, stderr=PIPE)
    if not result:
        raise ValueError(
            f"Bash command `wc -l {file_path}` returned and empty string. "
            f"Possibly, file {file_path} does not exist."
        )
    return int(result.stdout.decode('utf-8').split()[0])


def count_characters_in_file(file_path):
    with file_path.open() as f:
        count = sum(count_in_blocks(f))
    return count


def count_pages_in_file(file_path, start, num_characters):
    with file_path.open() as f:
        f.seek(start)
        count = sum(count_in_blocks(f, specific_to_count='<page', num_characters=num_characters))
    return count


def count_in_file_parts(file_path, part_num_characters, pattern):
    result = [0] * len(part_num_characters)
    num_preceding_characters_for_segment = list(accumulate(part_num_characters))
    current_segment_i = 0
    characters_read = 0
    buffer = 'filler'
    with file_path.open() as f:
        while buffer and num_preceding_characters_for_segment[current_segment_i] > characters_read:
            buffer = f.read(min(BUFFER_SIZE, num_preceding_characters_for_segment[current_segment_i] - characters_read))
            characters_read += len(buffer)
            result[current_segment_i] += buffer.count(pattern)
            if characters_read >= num_preceding_characters_for_segment[current_segment_i]:
                current_segment_i += 1
                if current_segment_i >= len(part_num_characters):
                    break
    return result


def move_by_n_characters_in_file(fd, n, buffer_size):
    characters_read = 0
    bytes_read = 0
    buffer = 'filler'
    while buffer and n > characters_read:
        buffer = fd.read(min(buffer_size, n - characters_read))
        characters_read += len(buffer)
        bytes_read += len(buffer.encode('utf-8'))
    return characters_read, bytes_read


def get_borders_with_documents_intact(file_path, num_parts):
    byte_borders = []
    num_characters_in_part = []
    length = count_characters_in_file(file_path)
    part_size = length // num_parts
    last_byte_border = 0
    total_characters_read = 0
    remainder = ""
    with file_path.open(buffering=BUFFER_SIZE) as f:
        for i in range(num_parts):
            read_size = part_size * (i + 1) - total_characters_read
            characters_in_part, bytes_read = move_by_n_characters_in_file(f, read_size, BUFFER_SIZE)
            characters_in_part += len(remainder)
            bytes_read += len(remainder.encode('utf-8'))
            total_characters_read += characters_in_part
            if characters_in_part < read_size:
                byte_borders.append((last_byte_border, last_byte_border + bytes_read))
                num_characters_in_part.append(characters_in_part)
            else:
                line = f.readline()
                total_characters_read += len(line)
                success = False
                while line:
                    if '<page' in line:
                        new_page_start = line.index('<page')
                        remainder = line[new_page_start:]
                        line = line[:new_page_start]
                        characters_in_part += len(line)
                        bytes_read += len(line.encode('utf-8'))
                        new_byte_border = last_byte_border + bytes_read
                        byte_borders.append((last_byte_border, new_byte_border))
                        num_characters_in_part.append(characters_in_part)
                        last_byte_border = new_byte_border
                        success = True
                        break
                    characters_in_part += len(line)
                    bytes_read += len(line.encode('utf-8'))
                    line = f.readline()
                    total_characters_read += len(line)
                if not success:
                    byte_borders.append((last_byte_border, last_byte_border + bytes_read))
                    num_characters_in_part.append(characters_in_part)
    return byte_borders, num_characters_in_part


def preprocess_wikipedia_parallel(
    num_jobs,
    file_path,
    output_dir,
    lang,
    tokenizer,
    start_doc_id=0,
    start_file_i=0,
    nltk_tokenization=True,
):
    logging.info("Calculating borders for multiprocessing...")
    byte_borders, num_characters_in_part = get_borders_with_documents_intact(file_path, num_jobs)
    logging.info(f"Found borders for multiprocessing: {byte_borders}")
    logging.info(f"Number of characters in parts: {num_characters_in_part}")
    num_output_files = [int(np.ceil(n / MAX_NUM_CHARACTERS_IN_1_FILE)) for n in num_characters_in_part]
    out_file_ids = list(accumulate(num_output_files, initial=start_file_i))
    logging.info(f"Calculating starting document ids for processes...")
    start_doc_ids = list(
        accumulate(
            # [count_pages_in_file(file_path, b[0], n) for b, n in zip(byte_borders, num_characters_in_part)],
            count_in_file_parts(file_path, num_characters_in_part, '<page'),
            initial=start_doc_id
        )
    )[:-1]
    logging.info(f"Starting document ids for processes are: {start_doc_ids}")
    logging.info(f"Calculating starting lines for processes...")
    start_line_ids = list(
        accumulate(
            # [count_lines_in_file(file_path, b[0], n) for b, n in zip(byte_borders, num_characters_in_part)],
            count_in_file_parts(file_path, num_characters_in_part, '\n'),
            initial=0
        )
    )[:-1]
    logging.info(f"Starting lines for processes are: {start_line_ids}")
    manager = mp.Manager()
    progress_queue = manager.Queue()
    logging.info("Creating progress process...")
    progress_process = mp.Process(
        target=big.show_prog,
        args=(progress_queue, count_lines_in_file_fast(file_path), "Lines"),
    )
    logging.info("Starting progress process...")
    progress_process.start()
    with mp.Pool(num_jobs) as pool:
        logging.info("Launching multiprocessing pool...")
        result = pool.starmap(
            preprocess_wikipedia,
            list(
                zip(
                    range(num_jobs),
                    [progress_queue] * num_jobs,
                    [file_path] * num_jobs,
                    byte_borders,
                    num_characters_in_part,
                    out_file_ids[:-1],
                    out_file_ids[1:],
                    num_output_files,
                    [output_dir] * num_jobs,
                    [lang] * num_jobs,
                    [tokenizer] * num_jobs,
                    start_doc_ids,
                    start_line_ids,
                    [nltk_tokenization] * num_jobs,
                )
            )
        )
    progress_queue.put(-1)
    progress_process.join()
    for i in range(1, len(result)):
        result[0].update(result[i])
    return result[0]


def preprocess_wikipedia(
    rank,
    progress_queue,
    file_path,
    byte_borders,
    num_characters_in_part,
    start_out_file_i,
    first_forbidden_out_file_i,
    num_out_files,
    output_dir,
    lang,
    tokenizer,
    start_doc_id,
    start_line_id,
    nltk_tokenization,
) -> Dict[int, int]:
    doc_id_to_file_i = {}
    page = ""
    page_i = start_doc_id
    page_in_progress = False
    characters_for_1_file = num_characters_in_part // num_out_files
    total_number_of_characters_from_original_text_in_current_file = 0
    file_i = start_out_file_i
    doc_id = start_doc_id
    output_dir.mkdir(exist_ok=True, parents=True)
    current_file_path = output_dir / (str(file_i) + '.xml')
    tok_chars, untok_chars = {'\n', ' '}, set()
    num_lines_processed_when_progress_was_reported_last_time = start_line_id
    start_line, end_line = None, None
    file_text = ""
    with file_path.open(buffering=BUFFER_SIZE) as in_f:
        in_f.seek(byte_borders[0])
        num_read_characters = 0
        for i, line in enumerate(in_f, num_lines_processed_when_progress_was_reported_last_time):
            if len(line) > num_characters_in_part - num_read_characters:
                line = line[:num_characters_in_part - num_read_characters]
            num_read_characters += len(line)
            if i % REPORT_PROGRESS_PERIOD == 0:
                progress_queue.put(i - num_lines_processed_when_progress_was_reported_last_time)
                num_lines_processed_when_progress_was_reported_last_time = i
            total_number_of_characters_from_original_text_in_current_file += len(line)
            if '<page' in line:
                if big.PAGE_OPENING_NORMAL_TAG.match(line) is None:
                    logging.warning(
                        f'Encountered an unusual page opening tag in line {i} {repr(line)} in process {rank}'
                    )
                page_in_progress = True
                start_line = i
            if page_in_progress:
                page += line
            if '</page' in line:
                if page_in_progress:
                    if big.PAGE_CLOSING_NORMAL_TAG.match(line) is None:
                        logging.warning(
                            f'Encountered an unusual page opening tag in line {i} {repr(line)} in process {rank}.'
                        )
                    elif page.count('\n') == 1:
                        logging.warning(
                            f"Encountered a page which takes only one line. Line: {i}. Line {repr(line)} in process"
                            f"{rank}."
                        )
                    end_line = i
                    title = big.TITLE_OF_PAGE.search(page)
                    if title is None:
                        logging.warning(f"Title of page {page_i} from line {start_line} to {end_line} is not found.")
                        title = None
                    else:
                        title = title.group(1)
                    if big.COLON_TITLES.match(title) is None and '(disambiguation)' not in title:
                        text = big.TEXT_OF_PAGE.search(page)
                        if text is None:
                            logging.warning(
                                f"Text tag is not found on a page {page_i} from line {start_line} to {end_line} "
                                f"in process {rank} is not found. Skipping page.."
                            )
                        else:
                            pos_info = [file_path, start_line, end_line]
                            text, tok_chars, untok_chars = big.get_wiki_text_lines(
                                text.group(1),
                                lang,
                                tokenizer,
                                tok_chars,
                                untok_chars,
                                pos_info,
                                nltk_tokenization,
                                remove_parentheses=False,
                            )
                            if text:
                                file_text += big.doc_to_str(
                                    doc_id, file_path, title, start_line, end_line, '\n'.join(text)
                                )
                                doc_id_to_file_i[doc_id] = file_i
                                doc_id += 1
                                if total_number_of_characters_from_original_text_in_current_file > characters_for_1_file:
                                    assert file_i < first_forbidden_out_file_i, f"File you are going to write into " \
                                        f"is probably filled in other process. There is an error in distribution of " \
                                        f"data between processes."
                                    with current_file_path.open('w') as out_f:
                                        out_f.write(file_text)
                                    file_text = ""
                                    file_i += 1
                                    current_file_path = output_dir / (str(file_i) + '.xml')
                                    total_number_of_characters_from_original_text_in_current_file = 0
                else:
                    logging.warning(
                        f'Encountered closing page tag without opening tag. Line number: {i}. Line {repr(line)} in '
                        f'process {rank}.'
                    )
                page = ""
                page_i += 1
                start_line = None
                end_line = None
                page_in_progress = False
            if num_read_characters >= num_characters_in_part:
                break
        if len(page) != 0:
            logging.warning(
                f"The page {page_i} with title {title} in file {file_path} between lines {start_line} and {end_line} "
                f"is not finished in process {rank}."
            )
    progress_queue.put(i + 1 - num_lines_processed_when_progress_was_reported_last_time)
    if total_number_of_characters_from_original_text_in_current_file:
        assert file_i < first_forbidden_out_file_i, f"File you are going to write into is probably filled in other " \
            f"process. There is an error in distribution of data between processes."
        with current_file_path.open('w') as out_f:
            out_f.write(file_text)
    return doc_id_to_file_i


def clean_small_dataset(docs, tokenizer, lang, file_path, corpus_type, normalize_and_check_quotes_and_parentheses):
    tok_chars = None
    untok_chars = None
    deleted_after_untokenizable_removal = 0
    deleted_after_suspicious_removal = 0
    number_of_removed_lines_because_of_untokenizable_characters = 0
    number_of_removed_suspicious_lines = 0
    for doc_id in tqdm(list(docs.keys()), total=len(docs), unit="doc", desc=f"Cleaning and normalizing {corpus_type}"):
        docs[doc_id]['text'], tok_chars, untok_chars, num_rem_lines = small.remove_untokenizable_characters_from_text(
            docs[doc_id]['text'], tokenizer, tok_chars, untok_chars, remove_entire_lines=True
        )
        number_of_removed_lines_because_of_untokenizable_characters += num_rem_lines
        if not docs[doc_id]['text']:
            deleted_after_untokenizable_removal += 1
        docs[doc_id]['text'] = big.BROKEN_PARENTHESES_WITH_CONTENT.sub(' ', docs[doc_id]['text'])
        docs[doc_id]['text'] = big.SPACE_DUP.sub(' ', docs[doc_id]['text'])
        not_empty = bool(docs[doc_id]['text'])
        after_suspicious_removal, num_rem_lines = big.remove_suspicious_lines_and_rearrange_quotes_and_spaces(
            docs[doc_id]['text'],
            normalize_and_check_quotes_and_parentheses=normalize_and_check_quotes_and_parentheses,
            check_suspicious_endings=False,
            check_suspicious_parentheses=False,
        )
        number_of_removed_suspicious_lines += num_rem_lines
        if not docs[doc_id]['text'] and not_empty:
            deleted_after_suspicious_removal += 1
        docs[doc_id]['text'] = big.normalize_punctuation(after_suspicious_removal, lang)
        docs[doc_id]['text'] = big.NEW_LINE_DUP.sub('\n', docs[doc_id]['text'])
        if not docs[doc_id]['text']:
            del docs[doc_id]
    logging.info(
        f"Number of documents from {corpus_type} file {file_path} which became empty after untokenizable removal: "
        f"{deleted_after_untokenizable_removal}, "
        f"after suspicious removal: {deleted_after_suspicious_removal}"
    )
    logging.info(
        f"Number of removed lines from {corpus_type} file {file_path} because of untokenizable characters: "
        f"{number_of_removed_lines_because_of_untokenizable_characters}. Number of removed suspicious lines: "
        f"{number_of_removed_suspicious_lines}."
    )
    return docs


def preprocess_europarl(
    file_path: Path,
    document_dir: Path,
    lang: str,
    start_doc_id: int,
    start_file_id: int,
    tokenizer: TokenizerSpec,
) -> Dict[int, int]:
    with file_path.open() as f:
        text = f.read()
    text = small.SPACING_CHARACTERS_TO_REPLACE.sub(' ', text)
    text_lines = text.splitlines()
    docs = {}
    doc_id = start_doc_id
    last_title = None
    for i, line in tqdm(enumerate(text_lines), total=len(text_lines), unit="line", desc="Processing europarl"):
        m = small.EUROPARL_LINE.match(line)
        if m is None:
            raise ValueError(f"Could not match {i} EUROPARL line {repr(line)}")
        text = m.group(1).strip()
        if (
            text
            and not small.too_many_digits(text)
            and small.WORD_WITH_PRECEDING_AND_FOLLOWING_PUNCTUATION.search(text) is not None
        ):
            text = small.EUROPARL_LSTRIP.sub('', text)
            title = "europarl_" + m.group(2).strip()
            title = title.replace('"', "'")
            if last_title is not None and last_title != title:
                docs[doc_id]['end_line'] = i
                doc_id += 1
            if doc_id not in docs:
                docs[doc_id] = {"text": text.strip() + '\n', "title": title, "source": file_path, "start_line": i}
            else:
                docs[doc_id]['text'] += text.strip() + '\n'
            last_title = title
    logging.info(f"Number of documents before final cleaning of europarl file {file_path}: {len(docs)}")
    if docs:
        docs[doc_id]['end_line'] = i + 1
    docs = clean_small_dataset(
        docs, tokenizer, lang, file_path, 'europarl', normalize_and_check_quotes_and_parentheses=False
    )
    if docs:
        logging.info(f"Number of documents after final cleaning of europarl file {file_path}: {len(docs)}")
        big.write_docs_to_file(docs, document_dir / (str(start_file_id) + '.xml'))
    else:
        logging.warning(f"Europarl file {file_path} gave no documents.")
    return {doc_id: start_file_id for doc_id in docs.keys()}


def preprocess_ted(
    file_path: Path, document_dir: Path, lang: str, start_doc_id: int, start_file_id: int, tokenizer: TokenizerSpec
) -> Dict[int, int]:
    with file_path.open() as f:
        original_text = f.read()
    text = small.SPACING_CHARACTERS_TO_REPLACE.sub(' ', original_text)
    soup = BeautifulSoup(text)
    docs = {}
    end_pos = 0
    end_line = 0
    ted_docs = list(soup.findAll("doc"))
    for doc_id, doc in tqdm(
        enumerate(soup.findAll("doc"), start=start_doc_id), total=len(ted_docs), unit="doc", desc="Processing TED"
    ):
        title = "TED_" + doc["docid"] + "._" + doc.find("title").text
        title = title.replace('"', "'")
        doc_text = ''.join([e for e in doc if isinstance(e, NavigableString)]).strip()
        lines = [
            line.strip() for line in doc_text.split('\n')
            if small.WORD_WITH_PRECEDING_AND_FOLLOWING_PUNCTUATION.search(line.strip()) is not None
        ]
        if lines:
            find_str = f'<doc docid="{doc["docid"]}"'
            start_pos = original_text.find(find_str, end_pos)
            assert start_pos >= 0, \
                f"Could not find string '{find_str}' in TED file {file_path} while processing document number " \
                f"{doc['docid']}. Starting to search from position {end_pos} (character number)."
            start_line = end_line + original_text[start_pos: end_pos].count('\n')
            end_pos = original_text.find('</doc>', start_pos)
            assert end_pos >= 0, \
                f"Could not find ending of document {doc_id} in TED file {file_path}. " \
                f"Starting to search from position {start_pos} (character number)."
            end_line = start_line + original_text[start_pos: end_pos].count('\n')
            docs[doc_id] = {
                'text': big.DOUBLE_SQUARE_BRACKETS_WITH_CONTENT.sub(' ', '\n'.join(lines) + '\n'),
                'title': title,
                'source': file_path,
                'start_line': start_line,
                'end_line': end_line,
            }
        else:
            logging.warning(f"Found empty document {doc_id} in TED dataset")
    docs = clean_small_dataset(
        docs, tokenizer, lang, file_path, 'TED', normalize_and_check_quotes_and_parentheses=False)
    if docs:
        logging.info(f"Number of documents after final cleaning of TED file {file_path}: {len(docs)}")
        big.write_docs_to_file(docs, document_dir / (str(start_file_id) + '.xml'))
    else:
        logging.warning(f"TED file {file_path} gave no documents.")
    return {doc_id: start_file_id for doc_id in docs.keys()}


def preprocess_rapid(
    file_path: Path, document_dir: Path, lang: str, start_doc_id: int, start_file_id: int, tokenizer: TokenizerSpec
) -> Dict[int, int]:
    with file_path.open() as f:
        original_text = f.read()
    text = small.SPACING_CHARACTERS_TO_REPLACE.sub(' ', original_text)
    soup = BeautifulSoup(text)
    docs = {}
    end_pos = 0
    end_line = 0
    rapid_files = list(soup.findAll("file"))
    for doc_id, file in tqdm(
        enumerate(rapid_files, start=start_doc_id), total=len(rapid_files), unit='doc', desc="Processing RAPID"
    ):
        title = "rapid_file_" + file["id"]
        lines = []
        for unit in file.findAll("unit"):
            unit_id = unit["id"]
            segment = unit.find("segment")
            source = segment.find("source")
            target = segment.find("target")
            if source['xml:lang'] == "en":
                text = source.text
            elif target["xml:lang"] == "en":
                text = target.text
            else:
                raise ValueError(
                    f"No utterance in English was found in file {file['id']} in unit {unit_id}. "
                    f"Source language: {source['lang']}. Target language: {target['lang']}"
                )
            if small.check_rapid_line(text):
                lines.append(small.SPACE_DUP.sub(' ', text.replace(chr(61623), ' ')).strip())
        if lines:
            find_str = f'<file id="{file["id"]}"'
            start_pos = original_text.find(find_str, end_pos)
            assert start_pos >= 0, \
                f"Could not find string '{find_str}' in TED file {file_path} while processing document number " \
                f"{file['id']}. Starting to search from position {end_pos} (character number)."
            start_line = end_line + original_text[start_pos: end_pos].count('\n')
            end_pos = original_text.find('</file>', start_pos)
            assert end_pos >= 0, \
                f"Could not find ending of document {doc_id} in TED file {file_path}. " \
                f"Starting to search from position {start_pos} (character number)."
            end_line = start_line + original_text[start_pos: end_pos].count('\n')
            docs[doc_id] = {
                'text': big.DOUBLE_SQUARE_BRACKETS_WITH_CONTENT.sub(' ', '\n'.join(lines) + '\n').strip(),
                'title': title,
                'source': file_path,
                'start_line': start_line,
                'end_line': end_line,
            }
    docs = clean_small_dataset(
        docs, tokenizer, lang, file_path, 'RAPID', normalize_and_check_quotes_and_parentheses=False)
    if docs:
        logging.info(f"Number of documents after final cleaning of RAPID file {file_path}: {len(docs)}")
        big.write_docs_to_file(docs, document_dir / (str(start_file_id) + '.xml'))
    else:
        logging.warning(f"TED file {file_path} gave no documents.")
    return {doc_id: start_file_id for doc_id in docs.keys()}


def preprocess_news_commentary(
    file_path: Path, document_dir: Path, lang: str, start_doc_id: int, start_file_id: int, tokenizer: TokenizerSpec
) -> Dict[int, int]:
    with file_path.open() as f:
        original_text = f.read()
    docs = {}
    discussion_lines = []
    discussion_count = 0
    line_idx = 0
    text_lines = small.SPACING_CHARACTERS_TO_REPLACE.sub(' ', original_text).splitlines(False)
    current_doc_id = start_doc_id
    start_line = 0
    for line_i, line in tqdm(
        enumerate(text_lines), total=len(text_lines), desc="Processing news-commentary", unit="line"
    ):
        line = line.strip()
        if line:
            if line_idx == 1:
                location_string = small.NEWS_COMMENTARY_LOCATION_LINE.match(line)
                if location_string is not None:
                    line = line[location_string.span()[1] :]
                line = line.strip()
                if line and small.MORE_THAN_10_HYPHENS.search(line) is None:
                    discussion_lines.append(line)
            elif line_idx > 1 and small.check_news_commentary_line(line):
                discussion_lines.append(line.lstrip('·* '))
            line_idx += 1
        else:
            if discussion_lines:
                docs[current_doc_id] = {
                    "text": '\n'.join(discussion_lines) + '\n',
                    "start_line": start_line,
                    "end_line": line_i,
                    "source": file_path,
                    "title": f"news-commentary_discussion{discussion_count}",
                }
                start_line = line_i
                discussion_count += 1
                current_doc_id += 1
            discussion_lines = []
            line_idx = 0
    if discussion_lines:
        docs[current_doc_id] = {
            "text": '\n'.join(discussion_lines) + '\n',
            "start_line": start_line,
            "end_line": line_i,
            "source": file_path,
            "title": f"news-commentary_discussion{discussion_count}",
        }
    docs = clean_small_dataset(
        docs, tokenizer, lang, file_path, 'news-commentary', normalize_and_check_quotes_and_parentheses=False)
    if docs:
        logging.info(f"Number of documents after final cleaning of news-commentary file {file_path}: {len(docs)}")
        big.write_docs_to_file(docs, document_dir / (str(start_file_id) + '.xml'))
    else:
        logging.warning(f"News-commentary file {file_path} gave no documents.")
    return {doc_id: start_file_id for doc_id in docs.keys()}


def tokenizability_initializer():
    global tok_chars
    global untok_chars
    tok_chars = None
    untok_chars = None


class WikiExtractedWorker:
    def __init__(self, document_dir: Path, lang: str, tokenizer: TokenizerSpec, progress_queue: mp.Queue):
        self.document_dir = document_dir
        self.lang = lang
        self.tokenizer = tokenizer
        self.progress_queue = progress_queue

    def prepare_wiki_extracted_doc(
        self, doc: str, start_line: int, end_line: int, input_file: Path
    ) -> Dict[str, Union[str, int, Path]]:
        header_match = WIKI_EXTRACTED_HEADER.search(doc)
        if header_match is None:
            raise ValueError(
                f"Document header is not found in file {input_file} for document in lines between {start_line} and "
                f"{end_line}"
            )
        title = header_match.group(3)
        doc = doc[header_match.span()[1]:]
        doc = doc.strip()
        first_end_line = doc.find('\n')
        doc = doc[first_end_line:].strip()
        doc = big.NEW_LINE_DUP.sub('\n', doc)
        doc = small.SPACING_CHARACTERS_TO_REPLACE.sub(' ', doc)
        global tok_chars
        global untok_chars
        doc, tok_chars, untok_chars, _ = small.remove_untokenizable_characters_from_text(
            doc, self.tokenizer, tok_chars, untok_chars, remove_entire_lines=True
        )
        doc = big.BROKEN_PARENTHESES_WITH_CONTENT.sub(' ', doc)
        doc = big.SPACE_DUP.sub(' ', doc)
        after_suspicious_removal, _ = big.remove_suspicious_lines_and_rearrange_quotes_and_spaces(
            doc,
            normalize_and_check_quotes_and_parentheses=True,
            check_suspicious_endings=True,
            check_suspicious_parentheses=True,
        )
        doc = big.normalize_punctuation(after_suspicious_removal, self.lang)
        doc = big.NEW_LINE_DUP.sub('\n', doc)
        doc = [sent.strip() for sent in doc.split('\n')]
        doc = [sent for sent in doc if sent.count(' ') > 4]
        doc = '\n'.join(doc)
        return {"text": doc, "start_line": start_line, "end_line": end_line, "source": input_file, "title": title}

    def __call__(self, input_file: Path, file_id: int, start_doc_id: int) -> None:
        with input_file.open() as f:
            text = f.read()
        docs = text.split('</doc>')
        prepared_docs = {}
        start_line = 0
        doc_count = 0
        for doc_id, doc in enumerate(docs, start=start_doc_id):
            num_lines = doc.count('\n')
            if WIKI_EXTRACTED_NOT_EMPTY_DOC.match(doc.lstrip()):
                prepared_doc = self.prepare_wiki_extracted_doc(
                    doc, start_line, start_line + num_lines, input_file
                )
                if prepared_doc['text']:
                    prepared_docs[doc_id] = prepared_doc
                doc_count += 1
                if doc_count % WIKI_EXTRACTED_DOC_PROGRESS_PERIOD == 0:
                    self.progress_queue.put(doc_count)
                    doc_count = 0
            start_line += num_lines
        self.progress_queue.put(doc_count)
        big.write_docs_to_file(prepared_docs, self.document_dir / (str(file_id) + '.xml'))


def count_not_empty_docs_in_file(file_path: Path, progress_queue: mp.Queue) -> int:
    with file_path.open() as f:
        text = f.read()
    docs = text.split('</doc>')
    num_not_empty = 0
    for doc in docs:
        if WIKI_EXTRACTED_NOT_EMPTY_DOC.match(doc.lstrip()):
            num_not_empty += 1
    progress_queue.put(1)
    return num_not_empty


def preprocess_wiki_extracted(
    dir_path: Path,
    document_dir: Path,
    lang: str,
    start_doc_id: int,
    start_file_id: int,
    tokenizer: TokenizerSpec,
    num_jobs: int,
) -> Dict[int, int]:
    files_with_data = [
        file for inner_dir in dir_path.iterdir() if inner_dir.is_dir()
        for file in inner_dir.iterdir() if file.stem.startswith('wiki')
    ]
    with Progress(
        len(files_with_data), "Counting not empty documents in extracted Wikipedia", "file"
    ) as progress_queues:
        with mp.Pool(num_jobs) as pool:
            num_not_empty_docs_in_files = pool.starmap(
                count_not_empty_docs_in_file, zip(files_with_data, [progress_queues[0]] * len(files_with_data))
            )
    start_doc_ids = list(itertools.accumulate(num_not_empty_docs_in_files, initial=start_doc_id))
    file_id_values = list(range(start_file_id, start_file_id + len(files_with_data)))
    with Progress(start_doc_ids[-1], "Preparing extracted Wikipedia", "doc") as progress_queues:
        with mp.Pool(num_jobs, initializer=tokenizability_initializer) as pool:
            pool.starmap(
                WikiExtractedWorker(document_dir, lang, tokenizer, progress_queues[0]),
                zip(files_with_data, file_id_values, start_doc_ids),
            )
    return {
        doc_id: file_id
        for i, file_id in enumerate(file_id_values)
        for doc_id in range(start_doc_ids[i], start_doc_ids[i+1])
    }


def split_large_files_into_small_files(
    input_dir: Path, output_dir: Path, num_lines_per_file: int
) -> Tuple[List[Path], List[Path], List[int], List[int]]:
    new_file_count = 0
    split_files, source_files, start_lines, end_lines, processes, opened_files = [], [], [], [], [], []
    for i, input_file in enumerate(input_dir.iterdir()):
        num_lines_in_input_file = count_lines_in_file(input_file)
        print(f"Number of lines in file {input_file}:", num_lines_in_input_file)
        print("num_lines_per_file:", num_lines_per_file)
        for start in range(0, num_lines_in_input_file, num_lines_per_file):
            new_file = output_dir / f"{new_file_count}.txt"
            split_files.append(new_file)
            source_files.append(input_file)
            start_lines.append(start)
            end_lines.append(min(num_lines_per_file, num_lines_in_input_file - start))
            opened_files.append(new_file.open('w'))
            processes.append(
                Popen(
                    [
                        'sed',
                        '-n',
                        f'{start + 1},{start + min(num_lines_per_file, num_lines_in_input_file - start)}p',
                        str(input_file),
                    ],
                    stdout=opened_files[-1],
                )
            )
            new_file_count += 1
    for proc in processes:
        proc.wait()
    for f in opened_files:
        f.close()
    return split_files, source_files, start_lines, end_lines


class NewsCrawlWorker:
    def __init__(self, document_dir: Path, lang: str, tokenizer: TokenizerSpec, progress_queue: mp.Queue):
        self.document_dir = document_dir
        self.lang = lang
        self.tokenizer = tokenizer
        self.progress_queue = progress_queue

    def __call__(
        self, input_file: Path, file_id: int, doc_id: int, source_file: Path, start_line: int, end_line: int, idx: int
    ) -> None:
        with input_file.open() as f:
            text = f.read()
        text = small.SPACING_CHARACTERS_TO_REPLACE.sub(' ', text)
        global tok_chars
        global untok_chars
        text, tok_chars, untok_chars, _ = small.remove_untokenizable_characters_from_text(
            text, self.tokenizer, tok_chars, untok_chars, remove_entire_lines=True
        )
        text = big.TRAILING_PARENTHESES.sub(' ', text)
        text = big.TRAILING_PARENTHESES.sub(' ', text)
        text = big.BROKEN_PARENTHESES_WITH_CONTENT.sub(' ', text)
        text = big.SPACE_DUP.sub(' ', text)
        after_suspicious_removal, _ = big.remove_suspicious_lines_and_rearrange_quotes_and_spaces(
            text,
            normalize_and_check_quotes_and_parentheses=True,
            check_suspicious_endings=True,
            check_suspicious_parentheses=True,
        )
        text = big.normalize_punctuation(after_suspicious_removal, self.lang)
        text = big.NEW_LINE_DUP.sub('\n', text)
        text = [sent.strip() for sent in text.split('\n')]
        text = [sent for sent in text]
        text = '\n'.join(text)
        if not text:
            return
        prepared_docs = {
            doc_id: {
                "text": text + ('' if text[-1] == '\n' else '\n'),
                "start_line": start_line,
                "end_line": end_line,
                "source": input_file,
                "title": f"news-crawl-{idx}",
            }
        }
        self.progress_queue.put(1)
        big.write_docs_to_file(prepared_docs, self.document_dir / (str(file_id) + '.xml'))


def preprocess_news_crawl(
    dir_path: Path,
    document_dir: Path,
    lang: str,
    start_doc_id: int,
    start_file_id: int,
    tokenizer: TokenizerSpec,
    num_jobs: int,
) -> Dict[int, int]:
    with TemporaryDirectory() as tmp_dir:
        tmp_files, source_files, start_lines, end_lines = split_large_files_into_small_files(
            dir_path, Path(tmp_dir), NUM_LINES_PER_NEWS_CRAWL_TMP_FILE
        )
        doc_ids = list(range(start_doc_id, start_doc_id + len(tmp_files)))
        file_ids = list(range(start_file_id, start_file_id + len(tmp_files)))
        with Progress(len(tmp_files), "Preparing news-crawl", "doc") as progress_queues:
            with mp.Pool(num_jobs, initializer=tokenizability_initializer) as pool:
                pool.starmap(
                    NewsCrawlWorker(document_dir, lang, tokenizer, progress_queues[0]),
                    zip(tmp_files, file_ids, doc_ids, source_files, start_lines, end_lines, range(len(tmp_files))),
                )
    return dict(zip(doc_ids, file_ids))


class PG19Worker:
    def __init__(self, document_dir: Path, progress_queue: mp.Queue) -> None:
        self.document_dir = document_dir
        self.progress_queue = progress_queue

    def __call__(self, file: Path, file_id: int, doc_id: int, idx: int) -> None:
        with file.open() as f:
            original_text = f.read()
        text = big.ALL_PARENTHESES.sub(' ', SQUARE_BRACKETS_PATTERN.sub(' ', original_text))
        paragraphs = [
            NEW_LINE_WITH_SPACES_PATTERN.sub(' ', p).strip() for p in SEVERAL_NEW_LINES_PATTERN.split(text)
            if len(p) > PG_19_MIN_PARAGRAPH_LEN and LIST_PATTERN.search(p) is None
        ]
        paragraphs = [UNDERSCORE_PATTERN.sub(r'\1', DOUBLE_HYPHEN_PATTERN.sub(' - ', p)) for p in paragraphs]
        paragraphs = [p for p in paragraphs if WORD_CHAR_ENDING_PATTERN.search(p) is None]
        text = '\n'.join(paragraphs) + '\n'
        text, _ = big.remove_suspicious_lines_and_rearrange_quotes_and_spaces(
            text,
            normalize_and_check_quotes_and_parentheses=True,
            check_suspicious_endings=False,
            check_suspicious_parentheses=True,
        )
        text = big.normalize_punctuation(text, 'en')
        text = big.NEW_LINE_DUP.sub('\n', text)
        if not text.strip():
            return
        text = text.lstrip() + ('' if text[-1] == '\n' else '\n')
        prepared_docs = {
            doc_id: {
                "text": text,
                "start_line": 0,
                "end_line": original_text.count('\n'),
                "source": file,
                "title": f"pg19-{idx}",
            }
        }
        self.progress_queue.put(1)
        big.write_docs_to_file(prepared_docs, self.document_dir / (str(file_id) + '.xml'))


def reverse_doc_id_to_file_i(doc_id_to_file_i: Dict[int, int]) -> Dict[int, List[int]]:
    rev = {}
    for k, v in doc_id_to_file_i.items():
        if v in rev:
            rev[v].append(k)
        else:
            rev[v] = [k]
    return rev


def rev_rev_doc_id_to_file_i(rev: Dict[int, List[int]]) -> Dict[int, int]:
    rev_rev = {}
    for k, v in rev.items():
        for vv in v:
            rev_rev[vv] = k
    return rev_rev


def merge_small_files(document_dir: Path, doc_id_to_file_i: Dict[int, int]) -> Dict[int, int]:
    rev = reverse_doc_id_to_file_i(doc_id_to_file_i)
    files = sorted(
        [f for f in document_dir.iterdir() if is_int(f.stem) and f.suffixes == ['.xml']], key=lambda x: int(x.stem)
    )
    stats = [f.stat().st_size for f in files]
    max_size = max(stats)
    curr_updated_file = files[0]
    curr_file_i = int(curr_updated_file.stem)
    curr_fo = curr_updated_file.open('a')
    curr_size = stats[0]
    for i in range(1, len(files)):
        if curr_size >= max_size:
            curr_fo.close()
            curr_updated_file = files[i]
            curr_file_i = int(curr_updated_file.stem)
            curr_fo = curr_updated_file.open('a')
            curr_size = stats[i]
        else:
            file_i = int(files[i].stem)
            with files[i].open() as f:
                curr_fo.write(f.read())
            curr_size += stats[i]
            rev[curr_file_i] += rev[file_i]
            del rev[file_i]
            files[i].unlink()
    curr_fo.close()
    return rev_rev_doc_id_to_file_i(rev)


def preprocess_pg19(
    dir_path: Path,
    document_dir: Path,
    start_doc_id: int,
    start_file_id: int,
    num_jobs: int,
) -> Dict[int, int]:
    files = list(dir_path.iterdir())
    nf = len(files)
    doc_ids = list(range(start_doc_id, start_doc_id + nf))
    file_ids = list(range(start_file_id, start_file_id + nf))
    with Progress(nf, "Preparing PG-19", "doc") as progress_queues:
        with mp.Pool(num_jobs) as pool:
            pool.starmap(
                PG19Worker(document_dir, progress_queues[0]),
                zip(files, file_ids, doc_ids, range(nf)),
            )
    doc_id_to_file_i = dict(zip(doc_ids, file_ids))
    return merge_small_files(document_dir, doc_id_to_file_i)


def is_sent_plausible(sent: str) -> bool:
    words_and_punc = small.WORD.split(sent)
    words, punc = [], []
    for elem in words_and_punc:
        if small.WORD.match(elem):
            words.append(elem)
        else:
            punc.append(elem)
    nw = len(words)
    if nw == 0:
        return True
    nw_without_letters = 0
    for w in words:
        if LETTER.search(w) is None:
            nw_without_letters += 1
    if nw_without_letters / nw > MAX_FRACTION_OF_WORDS_WITHOUT_LETTERS and nw > MIN_NUM_WORDS_FOR_FRACTION_CRITERIA:
        return False
    if sent.count('.') / nw > MAX_QUOTIENT_OF_NUMBER_OF_DOTS_IN_SENTENCE and nw > MIN_NUM_WORDS_FOR_FRACTION_CRITERIA:
        return False
    return True


class PubMedWorker:
    def __init__(self, document_dir: Path, tokenizer: TokenizerSpec, progress_queue: mp.Queue) -> None:
        self.document_dir = document_dir
        self.tokenizer = tokenizer
        self.progress_queue = progress_queue

    def __call__(self, file: Path, file_id: int, doc_id: int, idx: int) -> None:
        with file.open() as f:
            try:
                original_text = f.read()
            except UnicodeDecodeError:
                try:
                    with file.open('r', encoding='ISO-8859-1') as f_iso:
                        original_text = f_iso.read()
                except UnicodeDecodeError:
                    with file.open('rb') as fb:
                        blob = fb.read()
                    encoding = chardet.detect(blob)['encoding']
                    try:
                        original_text = blob.decode(encoding)
                    except UnicodeDecodeError:
                        logging.warning(
                            f"Could not decode file {file} using 'utf-8' and 'ISO-8859-1' and determined by `chardet` "
                            f"encoding '{encoding}'. Skipping file {file}."
                        )
                        return
        original_text = small.SPACING_CHARACTERS_TO_REPLACE.sub(' ', original_text)
        text = UPPERCASE_INTRO.sub(r'\1', big.ALL_PARENTHESES.sub(' ', SQUARE_BRACKETS_PATTERN.sub(' ', original_text)))
        paragraphs = SEVERAL_NEW_LINES_PATTERN.split(text)
        paragraphs = [SHORT_LINE.sub('\n', p) if p.count('\n') > 1 else p for p in paragraphs]
        paragraphs = [
            NEW_LINE_WITH_SPACES_PATTERN.sub(' ', p).strip() for p in paragraphs
            if len(p) > PG_19_MIN_PARAGRAPH_LEN and LIST_PATTERN.search(p) is None
        ]
        paragraphs = [UNDERSCORE_PATTERN.sub(r'\1', DOUBLE_HYPHEN_PATTERN.sub(' - ', p)) for p in paragraphs]
        paragraphs = [p for p in paragraphs if WORD_CHAR_ENDING_PATTERN.search(p) is None]
        new_paragraphs = []
        global tok_chars
        global untok_chars
        for p in paragraphs:
            ps = nltk.sent_tokenize(p)
            ps, _ = big.remove_suspicious_lines_and_rearrange_quotes_and_spaces(
                '\n'.join(ps),
                normalize_and_check_quotes_and_parentheses=True,
                check_suspicious_endings=False,
                check_suspicious_parentheses=True,
            )
            ps, tok_chars, untok_chars, _ = small.remove_untokenizable_characters_from_text(
                ps, self.tokenizer, tok_chars, untok_chars, remove_entire_lines=True
            )
            ps = ps.split('\n')
            ps = [sent for sent in ps if is_sent_plausible(sent)]
            new_paragraphs.append(' '.join(ps))
        text = '\n'.join(new_paragraphs) + '\n'
        text = big.normalize_punctuation(text, 'en')
        text = big.NEW_LINE_DUP.sub('\n', text)
        if not text.strip():
            return
        text = text.lstrip() + ('' if text[-1] == '\n' else '\n')
        prepared_docs = {
            doc_id: {
                "text": text,
                "start_line": 0,
                "end_line": original_text.count('\n'),
                "source": file,
                "title": f"pubmed-{idx}",
            }
        }
        self.progress_queue.put(1)
        big.write_docs_to_file(prepared_docs, self.document_dir / (str(file_id) + '.xml'))


def preprocess_pubmed(
    dir_path: Path,
    document_dir: Path,
    start_doc_id: int,
    start_file_id: int,
    tokenizer: TokenizerSpec,
    num_jobs: int,
) -> Dict[int, int]:
    files = []
    for d in dir_path.iterdir():
        files += list(d.iterdir())
    nf = len(files)
    doc_ids = list(range(start_doc_id, start_doc_id + nf))
    file_ids = list(range(start_file_id, start_file_id + nf))
    with Progress(nf, "Preparing PubMed", "doc") as progress_queues:
        with mp.Pool(num_jobs, initializer=tokenizability_initializer) as pool:
            pool.starmap(
                PubMedWorker(document_dir, tokenizer, progress_queues[0]),
                zip(files, file_ids, doc_ids, range(nf)),
            )
    doc_id_to_file_i = dict(zip(doc_ids, file_ids))
    return merge_small_files(document_dir, doc_id_to_file_i)


class GoogleNormalizationWorker:
    def __init__(self, document_dir: Path, tokenizer: TokenizerSpec, progress_queue: mp.Queue) -> None:
        self.document_dir = document_dir
        self.progress_queue = progress_queue
        self.tokenizer = tokenizer

    def __call__(self, file: Path, file_id: int, doc_id: int, idx: int) -> None:
        n_orig_lines = 0
        lines = []
        with file.open() as f:
            current_line = ""
            for line in f:
                n_orig_lines += 1
                parts = line.split()
                if parts[0] == '<eos>':
                    if count_words(current_line) >= GOOGLE_NORMALIZATION_DATASET_MIN_NUM_WORDS_IN_SENTENCE:
                        lines.append(current_line)
                    current_line = ""
                else:
                    if small.WORD_CHARACTER.match(parts[1]) is not None and current_line:
                        current_line += ' '
                    current_line += parts[1]
        text = '\n'.join(lines) + '\n'
        text = big.ALL_PARENTHESES.sub(' ', text)
        global tok_chars
        global untok_chars
        text, tok_chars, untok_chars, _ = small.remove_untokenizable_characters_from_text(
            text, self.tokenizer, tok_chars, untok_chars, remove_entire_lines=True
        )
        text, _ = big.remove_suspicious_lines_and_rearrange_quotes_and_spaces(
            text,
            normalize_and_check_quotes_and_parentheses=True,
            check_suspicious_endings=False,
            check_suspicious_parentheses=True,
        )
        text = big.SPACE_DUP.sub(' ', text)
        if not text.strip():
            return
        text = text + ('' if text[-1] == '\n' else '\n')
        text = big.normalize_punctuation(text, 'en')
        prepared_docs = {
            doc_id: {
                "text": text,
                "start_line": 0,
                "end_line": n_orig_lines,
                "source": file,
                "title": f"gnd-{idx}",
            }
        }
        self.progress_queue.put(1)
        big.write_docs_to_file(prepared_docs, self.document_dir / (str(file_id) + '.xml'))


def preprocess_google_normalization_dataset(
    dir_path: Path,
    document_dir: Path,
    start_doc_id: int,
    start_file_id: int,
    tokenizer: TokenizerSpec,
    num_jobs: int,
) -> Dict[int, int]:
    files = list(dir_path.iterdir())
    nf = len(files)
    doc_ids = list(range(start_doc_id, start_doc_id + nf))
    file_ids = list(range(start_file_id, start_file_id + nf))
    with Progress(nf, "Preparing Google Normalization dataset", "doc") as progress_queues:
        with mp.Pool(num_jobs, initializer=tokenizability_initializer) as pool:
            pool.starmap(
                GoogleNormalizationWorker(document_dir, tokenizer, progress_queues[0]),
                zip(files, file_ids, doc_ids, range(nf)),
            )
    return dict(zip(doc_ids, file_ids))


class PreprocessEuroparlRawWorker:
    def __init__(self, document_dir: Path, tokenizer: TokenizerSpec, progress_queue: mp.Queue) -> None:
        self.document_dir = document_dir
        self.progress_queue = progress_queue
        self.tokenizer = tokenizer

    def __call__(self, file: Path, file_id: int, doc_id: int, idx: int) -> None:
        with file.open() as f:
            text = f.read()
        n_orig_lines = text.count('\n') + (text[-1] != '\n')
        chapters = []
        for chapter in text.split('\n<CHAPTER'):
            start = chapter.find('<SPEAKER')
            if start >= 0:
                chapters.append(chapter[start:])
        chapters = [chapter[chapter.find('<SPEAKER'):] for chapter in text.split('<CHAPTER')]
        text = '\n'.join(chapters)
        text = EUROPARL_RAW_SPEAKER_LINE.sub('', text)
        text = EUROPARL_RAW_LANG_DISCLAIMER.sub('', text)
        text = EUROPARL_RAW_REPORTED_SPEECH.sub('', text).replace('\n<P>\n', ' ')
        text = big.ALL_PARENTHESES.sub(' ', text)
        global tok_chars
        global untok_chars
        text, tok_chars, untok_chars, _ = small.remove_untokenizable_characters_from_text(
            text, self.tokenizer, tok_chars, untok_chars, remove_entire_lines=True
        )
        text, _ = big.remove_suspicious_lines_and_rearrange_quotes_and_spaces(
            text,
            normalize_and_check_quotes_and_parentheses=True,
            check_suspicious_endings=False,
            check_suspicious_parentheses=True,
        )
        text = big.SPACE_DUP.sub(' ', text)
        if not text.strip():
            return
        text += ('' if text[-1] == '\n' else '\n')
        text = big.normalize_punctuation(text, 'en')
        prepared_docs = {
            doc_id: {
                "text": text.lstrip(),
                "start_line": 0,
                "end_line": n_orig_lines,
                "source": file,
                "title": f"gnd-{idx}",
            }
        }
        self.progress_queue.put(1)
        big.write_docs_to_file(prepared_docs, self.document_dir / (str(file_id) + '.xml'))


def preprocess_europarl_raw(
    dir_path: Path,
    document_dir: Path,
    start_doc_id: int,
    start_file_id: int,
    tokenizer: TokenizerSpec,
    num_jobs: int,
) -> Dict[int, int]:
    files = list(dir_path.iterdir())
    nf = len(files)
    doc_ids = list(range(start_doc_id, start_doc_id + nf))
    file_ids = list(range(start_file_id, start_file_id + nf))
    with Progress(nf, "Preparing EuroParl raw dataset", "doc") as progress_queues:
        with mp.Pool(num_jobs, initializer=tokenizability_initializer) as pool:
            pool.starmap(
                PreprocessEuroparlRawWorker(document_dir, tokenizer, progress_queues[0]),
                zip(files, file_ids, doc_ids, range(nf)),
            )
    return dict(zip(doc_ids, file_ids))


class PreprocessUNWorker:
    def __init__(self, document_dir: Path, tokenizer: TokenizerSpec, progress_queue: mp.Queue) -> None:
        self.document_dir = document_dir
        self.progress_queue = progress_queue
        self.tokenizer = tokenizer

    def __call__(self, file: Path, file_id: int, doc_id: int, idx: int) -> None:
        with file.open() as f:
            text = f.read()
        n_orig_lines = text.count('\n') + (text[-1] != '\n')
        body_split_1 = text.split('<body>')
        if len(body_split_1) != 2:
            raise ValueError(f"Wrong number {len(body_split_1) - 1} of <body> tags were found in file {file}")
        body_split_2 = body_split_1[1].split('</body>')
        if len(body_split_2) != 2:
            raise ValueError(f"Wrong number {len(body_split_2) - 1} of </body> tags were found in file {file}")
        body = body_split_2[0]
        paragraphs = [p.split('</p>')[0].strip('\n') for p in UN_PARAGRAPH_START.split(body)[1:]]
        global tok_chars
        global untok_chars
        text = ""
        for p in paragraphs:
            sentences = [s.split('</s>')[0] for s in UN_SENTENCE_START.split(p)[1:]]
            if not sentences:
                continue
            sentences = '\n'.join(sentences)
            sentences = html.unescape(sentences)
            if UN_FORBIDDEN_ENUMERATION_START.search(sentences) is not None:
                continue
            sentences = sentences.split('\n')
            if any([ENUMERATION_START.match(s) for s in sentences[1:]]):
                continue
            sentences[0] = ENUMERATION_START.sub('', sentences[0])
            sentences = '\n'.join(sentences)
            sentences, tok_chars, untok_chars, _ = small.remove_untokenizable_characters_from_text(
                sentences, self.tokenizer, tok_chars, untok_chars, remove_entire_lines=True
            )
            sentences, num_removed_lines = big.remove_suspicious_lines_and_rearrange_quotes_and_spaces(
                sentences,
                normalize_and_check_quotes_and_parentheses=True,
                check_suspicious_endings=True,
                check_suspicious_parentheses=True,
            )
            if num_removed_lines > 0:
                continue
            sentences = big.SPACE_DUP.sub(' ', sentences)
            if not sentences.strip():
                continue
            sentences = big.normalize_punctuation(sentences, 'en')
            sentences = sentences.replace('\n', ' ')
            text += sentences + '\n'
        prepared_docs = {
            doc_id: {
                "text": text.lstrip(),
                "start_line": 0,
                "end_line": n_orig_lines,
                "source": file,
                "title": f"gnd-{idx}",
            }
        }
        self.progress_queue.put(1)
        big.write_docs_to_file(prepared_docs, self.document_dir / (str(file_id) + '.xml'))


def find_files(dir_path: Union[str, os.PathLike], regex: str) -> List[Path]:
    pattern = re.compile(regex)
    dir_path = Path(dir_path).expanduser()
    result = []
    for root, _, files in os.walk(str(dir_path)):
        for name in files:
            if pattern.search(name):
                result.append(Path(root) / name)
    return result


def preprocess_un(
    dir_path: Path,
    document_dir: Path,
    start_doc_id: int,
    start_file_id: int,
    tokenizer: TokenizerSpec,
    num_jobs: int,
) -> Dict[int, int]:
    files = find_files(dir_path, '.xml$')
    nf = len(files)
    doc_ids = list(range(start_doc_id, start_doc_id + nf))
    file_ids = list(range(start_file_id, start_file_id + nf))
    with Progress(nf, "Preparing UN dataset", "doc") as progress_queues:
        with mp.Pool(num_jobs, initializer=tokenizability_initializer) as pool:
            pool.starmap(
                PreprocessUNWorker(document_dir, tokenizer, progress_queues[0]),
                zip(files, file_ids, doc_ids, range(nf)),
            )
    return dict(zip(doc_ids, file_ids))


def preprocess_tatoeba(file_path, document_dir, doc_id, file_id) -> Dict[int, int]:
    logging.info("Processing tatoeba...")
    lines = []
    with file_path.open() as f:
        for line in f:
            words = line.split()
            lines.append(' '.join(words[2:]))
    text = '\n'.join(lines)
    prepared_docs = {
        doc_id: {
            "text": text,
            "start_line": 0,
            "end_line": len(lines),
            "source": file_path,
            "title": f"tatoeba",
        }
    }
    big.write_docs_to_file(prepared_docs, document_dir / (str(file_id) + '.xml'))
    return {doc_id: file_id}


def is_int(s):
    try:
        int(s)
    except ValueError:
        return False
    return True


def read_doc(fd):
    text = []
    line = fd.readline()
    while line and not line.startswith('</doc>'):
        text.append(line.strip())
        line = fd.readline()
    return text


def strip_segment(segment):
    segment = segment.rstrip('-')
    if segment.endswith(' "') or segment.endswith('('):
        segment = segment.rstrip('"(')
    segment = segment.rstrip(' ')
    return FORBIDDEN_PUNCTUATION_IN_THE_START_OF_SEGMENT.sub('', segment)


def remove_parentheses(rank, progress_queue, files, output_dir):
    for file in files:
        docs, num_raw_characters_by_docs = big.read_docs_from_file(file)
        for doc, num_raw_characters in zip(docs.values(), num_raw_characters_by_docs):
            doc['text'] = big.ALL_PARENTHESES_WITH_PRECEDING_AND_FOLLOWING_SPACES.sub('', doc['text'])
            progress_queue.put(num_raw_characters)
        big.write_docs_to_file(docs, output_dir / file.name)


def remove_parentheses_parallel(document_dir, output_dir, num_jobs):
    logging.info(f"Removing parentheses.")
    files = [f for f in document_dir.iterdir() if is_int(f.stem) and f.suffixes == ['.xml']]
    num_jobs = min(num_jobs, len(files))
    num_files_per_job = len(files) // num_jobs
    distributed_files = (
        [files[i * num_files_per_job: (i + 1) * num_files_per_job] for i in range(num_jobs - 1)]
        + [files[(num_jobs - 1) * num_files_per_job:]]
    )
    manager = mp.Manager()
    progress_queue = manager.Queue()
    progress_process = mp.Process(
        target=big.show_prog, args=(progress_queue, count_total_number_of_characters(files), "char")
    )
    progress_process.start()
    output_dir.mkdir(parents=True, exist_ok=True)
    with mp.Pool(num_jobs) as pool:
        pool.starmap(
            remove_parentheses,
            list(zip(range(num_jobs), [progress_queue] * num_jobs, distributed_files, [output_dir] * num_jobs)),
        )
    progress_queue.put(-1)
    progress_process.join()


def count_words(text):
    return len(small.WORD_WITH_PRECEDING_AND_FOLLOWING_PUNCTUATION.findall(text))


def generate_segment_location(sentences: List[str], max_length: int, num_segments: int, file: Path) -> List[int]:
    if num_segments < 0:
        raise ValueError(f"Number of cut segments cannot be negative whereas num_segments={num_segments}")
    if num_segments == 0:
        return []
    num_words = 0
    # Calculating the maximum number of start sentence. There have to be enough sentences after start sentence to form
    # even longest segment.
    sent_i = len(sentences) - 1
    while sent_i >= 0 and num_words < max_length:
        num_words += count_words(sentences[sent_i])
        sent_i -= 1
    if num_words < max_length:
        raise ValueError(f"Not enough words ({num_words}) in file {file} to cut a segment of length {max_length}")
    elif num_segments > sent_i + 1:
        raise ValueError(
            f"Not enough words in file {file} to cut {num_segments} segments with maximum number of words in "
            f"segment {max_length}. If {num_words} words are taken in the end of document, the number of "
            f"remaining sentences {sent_i + 1} is less than number of segments to cut."
        )
    try:
        start_sentences = sorted(random.sample(list(range(sent_i + 1)), num_segments))
    except ValueError:
        logging.info(f"max_length, num_segments, file, sent_i: {max_length}, {num_segments}, {file}, {sent_i}")
        raise
    assert len(start_sentences) == num_segments
    return start_sentences


def cut_segment(text, shift, num_words_in_segment):
    segment = ""
    for word_i, m in enumerate(small.WORD_WITH_PRECEDING_AND_FOLLOWING_PUNCTUATION.finditer(text)):
        if word_i < shift:
            continue
        if word_i >= num_words_in_segment + shift:
            break
        segment += m.group(0)
    return segment


def extract_dev_text_segments_worker(
    file: Path, segment_lengths: List[int], after_extraction_document_dir: Path, progress_queue: mp.Queue,
):
    num_segments = len(segment_lengths)
    if num_segments == 0:
        return []
    after_extraction_document_dir.mkdir(parents=True, exist_ok=True)
    output_file = after_extraction_document_dir / file.name
    segments = []
    docs = big.read_docs_from_file(file)[0]
    sentences = []
    doc_ids = []
    sent_indices = []
    for doc_id, doc in docs.items():
        doc['lines'] = doc['text'].splitlines()
        doc['to_exclude'] = set()
        if len(doc['lines']) == 0:
            logging.warning(f"Document {doc_id} in file {file} is empty.")
        sentences += doc['lines']
        doc_ids += [doc_id] * len(doc['lines'])
        sent_indices.extend(range(len(doc['lines'])))
    start_sentences = generate_segment_location(
        sentences, max(segment_lengths), num_segments, file
    )
    curr_segment_i = 0
    sentence_i = 0
    progress = 0
    excluded = set()
    while sentence_i < len(sentences):
        if curr_segment_i < len(start_sentences) and sentence_i == start_sentences[curr_segment_i]:
            num_words = count_words(sentences[start_sentences[curr_segment_i]])
            shift = random.randint(0, num_words // 2)
            num_words_raw = 0
            num_sentences_for_segment = 0
            while num_words_raw < shift + segment_lengths[curr_segment_i]:
                num_words_raw += count_words(sentences[sentence_i + num_sentences_for_segment])
                num_sentences_for_segment += 1
            segments.append(
                cut_segment(
                    ' '.join(sentences[sentence_i : sentence_i + num_sentences_for_segment]),
                    shift,
                    segment_lengths[curr_segment_i],
                )
            )
            for i in range(num_sentences_for_segment):
                docs[doc_ids[sentence_i + i]]['to_exclude'].add(sent_indices[sentence_i + i])
            excluded.update({sentence_i + i for i in range(num_sentences_for_segment)})
            curr_segment_i += 1
            progress += 1
            if progress >= 1:
                progress_queue.put(progress)
                progress = 0
        sentence_i += 1
    filtered_docs = {}
    for doc_id, doc in docs.items():
        lines = [line for i, line in enumerate(doc['lines']) if i not in doc['to_exclude']]
        if lines:
            doc = copy.deepcopy(doc)
            del doc['lines']
            del doc['to_exclude']
            doc['text'] = '\n'.join(lines) + '\n'
            filtered_docs[doc_id] = doc
    if filtered_docs:
        big.write_docs_to_file(filtered_docs, output_file)
    assert len(segments) == num_segments, f"{len(segments)} were cut whereas {num_segments} segments were expected."
    progress_queue.put(progress)
    return segments


def get_segment_lengths_by_files(
    num_segments_by_files: List[int], sequence_length_range: Tuple[int, int]
) -> List[List[int]]:
    segment_lengths_by_files = []
    all_lengths = list(range(sequence_length_range[0], sequence_length_range[1]))
    curr_length_idx = 0
    for i, ns in enumerate(num_segments_by_files):
        segment_lengths_for_file = []
        for _ in range(ns):
            segment_lengths_for_file.append(all_lengths[curr_length_idx])
            curr_length_idx = (curr_length_idx + 1) % len(all_lengths)
        segment_lengths_by_files.append(segment_lengths_for_file)
    return segment_lengths_by_files


def extract_dev_text_segments(
    document_dir: Path,
    after_extraction_document_dir: Path,
    output_dir: Path,
    dev_size: int,
    test_size: int,
    sequence_length_range: Tuple[int, int],
    num_jobs: int,
):
    files = [f for f in document_dir.iterdir() if is_int(f.stem) and f.suffixes == ['.xml']]
    num_segments_by_files = get_how_many_segments_to_cut_by_files(
        files, dev_size + test_size, sequence_length_range[1] - 1, num_jobs
    )
    num_jobs = min(num_jobs, len(files))
    segment_lengths_by_files = get_segment_lengths_by_files(num_segments_by_files, sequence_length_range)
    with Progress(dev_size + test_size, 'Cutting dev and test segments', 'segment') as progress_queues:
        with mp.Pool(num_jobs) as pool:
            result = pool.starmap(
                extract_dev_text_segments_worker,
                zip(
                    files,
                    segment_lengths_by_files,
                    [after_extraction_document_dir] * len(files),
                    [progress_queues[0]] * len(files),
                )
            )
    result = list(chain(*result))
    assert len(result) == dev_size + test_size, (
        f"{len(result)} segments were cut whereas {dev_size + test_size} segments were expected."
    )
    dev_segments = result[:dev_size]
    test_segments = result[dev_size:]
    dev_text_file = output_dir / 'dev_text.txt'
    test_text_file = output_dir / 'test_text.txt'
    with dev_text_file.open('w') as f:
        for segment in dev_segments:
            f.write(segment + '\n')
    with test_text_file.open('w') as f:
        for segment in test_segments:
            f.write(segment + '\n')
    return dev_text_file, test_text_file


def cut_and_save_one_pass(text, out_f, progress_queue, num_words_in_segments):
    permutation = random.sample(num_words_in_segments, len(num_words_in_segments))
    shift = random.randint(0, max(num_words_in_segments) // 2)
    p_i = 0
    start_match = None
    num_in_segment = 0
    progress_report = 0
    num_cut_segments = 0
    m = None
    for m in small.WORD_WITH_PRECEDING_AND_FOLLOWING_PUNCTUATION.finditer(text):
        if shift > 0:
            shift -= 1
            continue
        if start_match is None:
            start_match = m
        num_in_segment += 1
        if num_in_segment == permutation[p_i]:
            out_f.write(strip_segment(text[start_match.span()[0]: m.span()[1]]) + '\n')
            start_match = None
            p_i = (p_i + 1) % len(permutation)
            if p_i == 0:
                permutation = random.sample(num_words_in_segments, len(num_words_in_segments))
            progress_report += 1
            if progress_report >= REPORT_PROGRESS_PERIOD:
                progress_queue.put(progress_report)
                progress_report = 0
            num_in_segment = 0
            num_cut_segments += 1
    if start_match is not None:
        out_f.write(strip_segment(text[start_match.span()[0]: m.span()[1]]) + '\n')
        num_cut_segments += 1
    return num_cut_segments


def cut_and_save(file_num, progress_queue, file, num_passes_through_dataset, output_dir, sequence_range):
    out_file = output_dir / (file.stem + '.txt')
    text = list(big.read_docs_from_file(file)[0].items())
    random.shuffle(text)
    text = '\n'.join([doc[1]['text'] for doc in text])
    text = small.SPACE_DUP.sub(' ', text.replace('\n', ' '))
    num_words = count_words(text)
    if num_words < sequence_range[0] * 2:
        return
    num_words_in_segments = list(range(sequence_range[0], min(sequence_range[1], num_words // 2)))
    with out_file.open('w', buffering=BUFFER_SIZE) as out_f:
        for _ in range(num_passes_through_dataset):
            cut_and_save_one_pass(text, out_f, progress_queue, num_words_in_segments)


def get_max_allowed_segments_for_text(text: str, max_segment_length: int) -> int:
    sentences = text.splitlines()
    num_words = 0
    # Calculating the maximum number of start sentence. There have to be enough sentences after start sentence
    # to form even longest segment.
    sent_i = len(sentences) - 1
    while sent_i >= 0 and num_words < max_segment_length:
        num_words += count_words(sentences[sent_i])
        sent_i -= 1
    if num_words < max_segment_length:
        return 0
    return sent_i + 1


class GetMaxAllowedSegmentsPerFileWorker:
    def __init__(self, max_segment_length: int, progress_queue: mp.Queue) -> None:
        self.max_segment_length = max_segment_length
        self.progress_queue = progress_queue

    def __call__(self, file: Path) -> int:
        text = '\n'.join([doc['text'] for doc in big.read_docs_from_file(file)[0].values()])
        self.progress_queue.put(1)
        return get_max_allowed_segments_for_text(text, self.max_segment_length)


def get_how_many_segments_to_cut_by_files(
    files: List[Path], size: int, max_segment_length: int, num_jobs: int
) -> List[int]:
    stats = [f.stat().st_size for f in files]
    total_size = sum(stats)
    fracs = [s / total_size for s in stats]
    sizes = [round(f * size) for f in fracs]
    with Progress(len(files), "Calculating maximum number of segments from a file", "file") as progress_queues:
        with mp.Pool(num_jobs) as pool:
            max_possible_segments_per_file = pool.map(
                GetMaxAllowedSegmentsPerFileWorker(max_segment_length, progress_queues[0]), files
            )
    for i, s in enumerate(sizes):
        if s > max_possible_segments_per_file[i]:
            sizes[i] = max_possible_segments_per_file[i]
    sum_ = sum(sizes)
    if sum_ > size:
        permutation = random.sample(list(range(len(sizes))), len(sizes))
        i = 0
        while sum_ > size and i < len(permutation):
            if sizes[permutation[i]] > 0:
                sizes[permutation[i]] -= 1
                sum_ -= 1
            i += 1
    elif sum_ < size:
        permutation = random.sample(list(range(len(sizes))), len(sizes))
        was_increase = True
        while sum_ < size and was_increase:
            was_increase = False
            for j in permutation:
                if sizes[j] < max_possible_segments_per_file[j]:
                    sizes[j] += 1
                    was_increase = True
                    sum_ += 1
                    if sum_ >= size:
                        assert sum_ == size
                        break
        if sum_ < size:
            raise ValueError(
                f"Cannot cut required number of segments {size} from the dataset because there is no enough "
                f"large enough files. Maximum allowed number of segments to cut is {sum_}. You may reduce "
                f"number of segments required or cut them manually."
            )
    assert len(sizes) == len(files)
    assert all([s >= 0 for s in sizes])
    assert sum(sizes) == size
    return sizes


def estimate_number_of_segments(rank, progress_queue, files, sequence_length_range):
    num_words = 0
    for file_path in files:
        with file_path.open() as f:
            text = f.read()
            num_words += len(
                small.WORD_WITH_PRECEDING_AND_FOLLOWING_PUNCTUATION.findall(big.DOC_MARK_UP_LINES.sub('', text))
            )
            progress_queue.put(len(text))
    return (
        num_words
        // sum(range(sequence_length_range[0], sequence_length_range[1]))
        * (sequence_length_range[1] - sequence_length_range[0])
    )


def count_total_number_of_characters(files):
    num_characters = 0
    logging.info("Estimating number of characters in files...")
    for file_path in tqdm(files, unit='file'):
        with file_path.open() as f:
            num_characters += len(f.read())
    return num_characters


def estimate_number_of_segments_parallel(files, sequence_length_range, num_jobs):
    logging.info("Estimating number of segments in the resulting dataset...")
    num_jobs = min(num_jobs, len(files))
    num_files_per_job = len(files) // num_jobs
    distributed_files = (
        [files[i * num_files_per_job: (i + 1) * num_files_per_job] for i in range(num_jobs - 1)]
        + [files[(num_jobs - 1) * num_files_per_job:]]
    )
    manager = mp.Manager()
    progress_queue = manager.Queue()
    progress_process = mp.Process(
        target=big.show_prog, args=(progress_queue, count_total_number_of_characters(files), "char")
    )
    progress_process.start()
    with mp.Pool(num_jobs) as pool:
        res = pool.starmap(
            estimate_number_of_segments,
            list(
                zip(
                    range(num_jobs), [progress_queue] * num_jobs, distributed_files, [sequence_length_range] * num_jobs
                )
            )
        )
    progress_process.join()
    return sum(res)


def cut_and_save_parallel(document_dir, sorted_text_file, num_passes_through_dataset, sequence_length_range, num_jobs):
    files = [f for f in document_dir.iterdir() if is_int(f.stem) and f.suffixes == ['.xml']]
    num_jobs = min(num_jobs, len(files))
    manager = mp.Manager()
    progress_queue = manager.Queue()
    size = estimate_number_of_segments_parallel(files, sequence_length_range, num_jobs)
    progress_process = mp.Process(target=big.show_prog, args=(progress_queue, size, "segment"))
    progress_process.start()
    output_dir = sorted_text_file.parent / 'cut_separate_files'
    output_dir.mkdir(parents=True, exist_ok=True)
    with mp.Pool(num_jobs) as pool:
        pool.starmap(
            cut_and_save,
            list(
                zip(
                    range(len(files)),
                    [progress_queue] * len(files),
                    files,
                    [num_passes_through_dataset] * len(files),
                    [output_dir] * len(files),
                    [sequence_length_range] * len(files),
                )
            )
        )
    progress_queue.put(-1)
    progress_process.join()
    with sorted_text_file.open('w') as out_f:
        for p in output_dir.iterdir():
            with p.open() as in_f:
                if is_int(p.stem) and p.suffixes == ['.txt']:
                    text = in_f.read()
                    out_f.write(text + ('' if text[-1] == '\n' else '\n'))


def count_content_lines_in_files(files: List[Path], file_batch_size: int = 128) -> int:
    num_lines = []
    for batch_start in range(0, len(files), file_batch_size):
        batch_processes = []
        for input_file in files[batch_start: batch_start + file_batch_size]:
            batch_processes.append(Popen(['wc', '-l', str(input_file)], stdout=PIPE, stderr=PIPE))
        for proc in batch_processes:
            proc.wait()
        num_lines += [int(p.stdout.read().decode('utf-8').split()[0]) for p in batch_processes]
    num_docs = []
    for batch_start in range(0, len(files), file_batch_size):
        grep_batch_processes, wc_batch_processes = [], []
        for input_file in files[batch_start: batch_start + file_batch_size]:
            grep_batch_processes.append(Popen(['grep',  "<doc docid=", str(input_file)], stdout=PIPE))
            wc_batch_processes.append(Popen(['wc', '-l'], stdin=grep_batch_processes[-1].stdout, stdout=PIPE, stderr=PIPE))
        for grep_proc, wc_proc in zip(grep_batch_processes, wc_batch_processes):
            grep_proc.wait()
            outs, _ = wc_proc.communicate(input=''.encode('utf8'))
            num_docs.append(int(outs))
    total_lines = 0
    for nl, nd in zip(num_lines, num_docs):
        assert nl >= nd * 2
        total_lines += (nl - nd * 2)
    return total_lines


class CutIntactSentencesWorker:
    def __init__(self, output_dir: Path, progress_queue: mp.Queue, use_nltk_sentence_splitting: bool) -> None:
        self.output_dir = output_dir
        self.progress_queue = progress_queue
        self.use_nltk_sentence_splitting = use_nltk_sentence_splitting

    def __call__(self, file: Path) -> None:
        out_file = self.output_dir / (file.stem + '.txt')
        docs, _ = big.read_docs_from_file(file)
        line_count = 0
        with out_file.open('w') as f:
            for doc in docs.values():
                for line in doc['text'].splitlines():
                    if self.use_nltk_sentence_splitting:
                        for sent in nltk.sent_tokenize(line):
                            sent = sent.strip()
                            if small.WORD.search(sent):
                                f.write(sent.strip() + '\n')
                    else:
                        line = line.strip()
                        if small.WORD.search(line):
                            f.write(line + '\n')
                    line_count += 1
                    if line_count % INTACT_SENTENCES_PROGRESS_PERIOD == 0:
                        self.progress_queue.put(line_count)
                        line_count = 0
        self.progress_queue.put(line_count)


def cut_and_save_parallel_intact_sentences(
    document_dir: Path, sorted_text_file: Path, use_nltk_sentence_splitting: bool, num_jobs: int
) -> None:
    files = [f for f in document_dir.iterdir() if is_int(f.stem) and f.suffixes == ['.xml']]
    num_jobs = min(num_jobs, len(files))
    total_num_lines = count_content_lines_in_files(files)
    output_dir = sorted_text_file.parent / 'cut_separate_files'
    output_dir.mkdir(parents=True, exist_ok=True)
    with Progress(total_num_lines, "Cutting into segments", "line") as progress_queues:
        with mp.Pool(num_jobs) as pool:
            pool.map(CutIntactSentencesWorker(output_dir, progress_queues[0], use_nltk_sentence_splitting), files)
    with sorted_text_file.open('w') as out_f:
        for p in output_dir.iterdir():
            with p.open() as in_f:
                if is_int(p.stem) and p.suffixes == ['.txt']:
                    text = in_f.read()
                    out_f.write(text + ('' if text[-1] == '\n' else '\n'))


def shuffle_file_lines(input_file, output_file):
    with output_file.open('w') as f:
        run(['shuf', str(input_file)], stdout=f, check=True)


def join_sentence_len(di_ss_se, sentence_len_by_docs):
    return sum(sentence_len_by_docs[di_ss_se[0]][di_ss_se[1]: di_ss_se[2]])


def main():
    args = get_args(SUPPORTED_CORPUS_TYPES, add_resume_argument=True)
    document_dir = args.output_dir / Path("documents")
    if args.resume_from is None:
        tokenizer = get_tokenizer(args.tokenizer)
        doc_id_to_file_i = {}
        start_doc_id, start_file_id = 0, 0
        for corpus_type, file_or_dir_path in zip(args.corpus_types, args.input_files_or_dirs):
            if corpus_type == SUPPORTED_CORPUS_TYPES[0]:  # wikipedia
                logging.info(f"Preprocessing wikipedia file {file_or_dir_path}...")
                corpus_doc_id_to_file_i = preprocess_wikipedia_parallel(
                    args.num_jobs,
                    file_or_dir_path,
                    document_dir,
                    args.input_language,
                    tokenizer,
                    start_doc_id,
                    start_file_id,
                    args.nltk_tokenization,
                )
            elif corpus_type == SUPPORTED_CORPUS_TYPES[1]:  # europarl
                corpus_doc_id_to_file_i = preprocess_europarl(
                    file_or_dir_path, document_dir, args.input_language, start_doc_id, start_file_id, tokenizer,
                )
            elif corpus_type == SUPPORTED_CORPUS_TYPES[2]:  # TED
                corpus_doc_id_to_file_i = preprocess_ted(
                    file_or_dir_path, document_dir, args.input_language, start_doc_id, start_file_id, tokenizer,
                )
            elif corpus_type == SUPPORTED_CORPUS_TYPES[3]:  # rapid
                corpus_doc_id_to_file_i = preprocess_rapid(
                    file_or_dir_path, document_dir, args.input_language, start_doc_id, start_file_id, tokenizer,
                )
            elif corpus_type == SUPPORTED_CORPUS_TYPES[4]:  # news-commentary
                corpus_doc_id_to_file_i = preprocess_news_commentary(
                    file_or_dir_path, document_dir, args.input_language, start_doc_id, start_file_id, tokenizer,
                )
            elif corpus_type == SUPPORTED_CORPUS_TYPES[5]:  # wiki-extracted
                corpus_doc_id_to_file_i = preprocess_wiki_extracted(
                    file_or_dir_path,
                    document_dir,
                    args.input_language,
                    start_doc_id,
                    start_file_id,
                    tokenizer,
                    args.num_jobs,
                )
            elif corpus_type == SUPPORTED_CORPUS_TYPES[6]:  # news-crawl
                corpus_doc_id_to_file_i = preprocess_news_crawl(
                    file_or_dir_path,
                    document_dir,
                    args.input_language,
                    start_doc_id,
                    start_file_id,
                    tokenizer,
                    args.num_jobs,
                )
            elif corpus_type == SUPPORTED_CORPUS_TYPES[7]:  # pg19
                corpus_doc_id_to_file_i = preprocess_pg19(
                    file_or_dir_path, document_dir, start_doc_id, start_file_id, args.num_jobs
                )
                with open('PG-19_doc_id_to_file_i.json', 'w') as f:
                    import json
                    json.dump(corpus_doc_id_to_file_i, f)
            elif corpus_type == SUPPORTED_CORPUS_TYPES[8]:  # pubmed
                corpus_doc_id_to_file_i = preprocess_pubmed(
                    file_or_dir_path, document_dir, start_doc_id, start_file_id, tokenizer, args.num_jobs
                )
            elif corpus_type == SUPPORTED_CORPUS_TYPES[9]:  # google-normalization-dataset
                corpus_doc_id_to_file_i = preprocess_google_normalization_dataset(
                    file_or_dir_path, document_dir, start_doc_id, start_file_id, tokenizer, args.num_jobs
                )
            elif corpus_type == SUPPORTED_CORPUS_TYPES[10]:  # tatoeba
                corpus_doc_id_to_file_i = preprocess_tatoeba(
                    file_or_dir_path, document_dir, start_doc_id, start_file_id
                )
            elif corpus_type == SUPPORTED_CORPUS_TYPES[11]:  # europarl_raw
                corpus_doc_id_to_file_i = preprocess_europarl_raw(
                    file_or_dir_path, document_dir, start_doc_id, start_file_id, tokenizer, args.num_jobs
                )
            elif corpus_type == SUPPORTED_CORPUS_TYPES[12]:  # un
                corpus_doc_id_to_file_i = preprocess_un(
                    file_or_dir_path, document_dir, start_doc_id, start_file_id, tokenizer, args.num_jobs
                )
            else:
                raise ValueError(
                    f"Unsupported corpus type '{corpus_type}. Supported corpus types are {big.SUPPORTED_CORPUS_TYPES}"
                )
            doc_id_to_file_i.update(corpus_doc_id_to_file_i)
            start_doc_id = max(corpus_doc_id_to_file_i.keys()) + 1
            start_file_id = max(corpus_doc_id_to_file_i.values()) + 1
    if args.dev_size > 0 or args.test_size > 0:
        after_extraction_document_dir = args.output_dir / Path("after_extraction_documents")
    else:
        after_extraction_document_dir = document_dir
    if args.resume_from is None or args.resume_from in ['dev_test_extraction']:
        dev_text_file, test_text_file = extract_dev_text_segments(
            document_dir,
            after_extraction_document_dir,
            args.output_dir,
            args.dev_size,
            args.test_size,
            args.sequence_length_range,
            args.num_jobs,
        )
    else:
        dev_text_file, test_text_file = args.output_dir / 'dev_text.txt', args.output_dir / 'test_text.txt'
    sorted_text_file = args.output_dir / 'sorted_text.txt'
    if args.resume_from is None or args.resume_from in ["cutting"]:
        rp = '(' not in args.allowed_punctuation or ')' not in args.allowed_punctuation
        if rp:
            rp_dir = after_extraction_document_dir.parent / 'documents_without_parentheses'
            remove_parentheses_parallel(after_extraction_document_dir, rp_dir, args.num_jobs)
        else:
            rp_dir = None
        if args.intact_sentences:
            cut_and_save_parallel_intact_sentences(
                rp_dir if rp else after_extraction_document_dir,
                sorted_text_file,
                args.use_nltk_sentence_splitting,
                args.num_jobs,
            )
        else:
            cut_and_save_parallel(
                rp_dir if rp else after_extraction_document_dir,
                sorted_text_file,
                args.num_passes_through_dataset,
                args.sequence_length_range,
                args.num_jobs,
            )
    shuffled_text_file = args.output_dir / 'shuffled_text.txt'
    if args.resume_from is None or args.resume_from in ["cutting", "shuffling"]:
        logging.info("shuffling segments...")
        shuffle_file_lines(sorted_text_file, shuffled_text_file)
    size = count_lines_in_file(shuffled_text_file)
    logging.info(f"Train set will contain {size} lines.")
    args.output_dir.mkdir(parents=True, exist_ok=True)
    if args.test_size > 0:
        logging.info("Writing test dataset...")
        big.write_dataset_fast(
            [0, args.test_size],
            test_text_file,
            args.output_dir / Path("test"),
            args.create_model_input,
            args.bert_labels,
            args.autoregressive_labels,
            args.allowed_punctuation,
            args.only_first_punctuation_character_after_word_in_autoregressive,
            args.no_label_if_all_characters_are_upper_case,
        )
    if args.dev_size > 0:
        logging.info("Writing dev dataset...")
        big.write_dataset_fast(
            [0,  args.dev_size],
            dev_text_file,
            args.output_dir / Path("dev"),
            args.create_model_input,
            args.bert_labels,
            args.autoregressive_labels,
            args.allowed_punctuation,
            args.only_first_punctuation_character_after_word_in_autoregressive,
            args.no_label_if_all_characters_are_upper_case,
        )
    logging.info("Writing train dataset...")
    big.write_dataset_parallel(
        [0, size],
        shuffled_text_file,
        args.output_dir / Path("train"),
        args.create_model_input,
        args.bert_labels,
        args.autoregressive_labels,
        args.allowed_punctuation,
        args.only_first_punctuation_character_after_word_in_autoregressive,
        args.no_label_if_all_characters_are_upper_case,
        args.num_jobs,
    )


def get_args(
    supported_corpus_types, add_resume_argument=False,
):
    parser = argparse.ArgumentParser(formatter_class=argparse.ArgumentDefaultsHelpFormatter,)
    parser.add_argument(
        "--input_files_or_dirs",
        help="List of files or directories with input data. Directory is required only for 'wiki-extracted' corpus. "
        "It should be a directory with data created by WikiExtractor instrument. You should also provide "
        "`--corpus_types` list which elements are types of corpuses for corresponding files and directories.",
        nargs="+",
        type=Path,
        required=not add_resume_argument,
    )
    parser.add_argument(
        "--input_language",
        "-L",
        help="Used for punctuation normalization. en - English, de - German, cz - Czech, fr - French. "
        "Other options (List of supported languages https://fasttext.cc/docs/en/language-identification.html) are also "
        "possible but there is no special instructions for punctuation normalization. "
        "See https://github.com/moses-smt/mosesdecoder/blob/master/scripts/tokenizer/normalize-punctuation.perl",
        default="en",
    )
    parser.add_argument(
        "--output_dir",
        "-o",
        help="Path to the output dir with dev.txt, train.txt, and test.txt files.",
        type=Path,
        required=True,
    )
    parser.add_argument(
        "--corpus_types",
        "-c",
        help="List of names of WMT corpuses which is used as raw material for creating punctuation capitalization "
        "dataset. Number and order of elements in this list should be equal to the number of elements in "
        "`input_files_or_dirs` list.",
        choices=supported_corpus_types,
        nargs="+",
        required=True,
    )
    parser.add_argument(
        "--num_passes_through_dataset",
        "-S",
        type=int,
        help="How many times the script goes through data to cut train segments. Dev and test are cut train and "
        "sentences used for dev and test are excluded from the process. This parameter is not used if "
        "`--intact_sentences` is set.",
        default=1,
    )
    parser.add_argument("--dev_size", "-d", help="Number of sequences in dev data.", type=int, default=10 ** 4)
    parser.add_argument("--test_size", "-t", help="Percentage of test data.", type=int, default=10 ** 4)
    parser.add_argument(
        "--sequence_length_range",
        "-r",
        help="Minimum and maximum number words in model input sequences. Number of words is sampled "
        "using uniform distribution. This parameter is not used if `--intact_sentences` is set.",
        type=int,
        nargs=2,
        default=(2, 64),
    )
    parser.add_argument(
        "--intact_sentences",
        help="Whether to split text into intact sentences instead of cutting them using parameters "
        "`--num_passes_through_dataset` and `--sequence_length_range`. If this parameter is provided, then you have "
        "provide `--use_nltk_sentence_splitting` parameter.",
        action="store_true",
    )
    parser.add_argument(
        "--use_nltk_sentence_splitting",
        help="Whether to apply NLTK sentence tokenization before writing intact sentences. This parameter works only "
        "if `--intact_sentences` is provided.",
        action="store_true",
    )
    parser.add_argument(
        "--create_model_input",
        "-i",
        help="Whether to write text without punctuation to output directory",
        action="store_true",
    )
    parser.add_argument("--bert_labels", "-b", help="Whether create BERT labels.", action="store_true")
    parser.add_argument(
        "--autoregressive_labels", "-a", help="Whether create autoregressive labels", action="store_true"
    )
    parser.add_argument(
        "--allowed_punctuation",
        "-p",
        help=f"A string containing punctuation marks on which training is performed. Example: '.,?'. "
        f"Do not include single quote and space into it. If single quotes are included they will be ignored. "
        f"BERT labels can include only {small.SUPPORTED_BERT_PUNCTUATION} punctuation characters.",
        type=set,
        default=set('"!(),-.:;?'),
    )
    parser.add_argument(
        "--tokenizer",
        "-z",
        help="Tokenizer used for checking characters for tokenizability.",
        default="bert-base-uncased",
    )
    parser.add_argument(
        "--only_first_punctuation_character_after_word_in_autoregressive",
        "-F",
        help="Add only first punctuation character after word to autoregressive labels.",
        action="store_true",
    )
    parser.add_argument(
        "--no_label_if_all_characters_are_upper_case",
        "-U",
        help="If this option is set all words capitalization are labelled as 'U' if the first character is in upper "
        "case. If this option is not set words which contain only uppercase letters (except one character words) "
        "are marked as 'U' and words which first character is in upper case but containing not lower case characters "
        "are marked as 'u'.",
        action="store_true",
    )
    parser.add_argument(
        "--nltk_tokenization",
        "-n",
        help="Tokenize lines into sentences using NLTK tokenization.",
        action="store_true",
    )
    parser.add_argument(
        "--resume_from",
        choices=["cutting", "shuffling", "writing"],
        help="From which stage big dataset preparation is started."
    )
    parser.add_argument("--num_jobs", default=1, type=int)
    args = parser.parse_args()
    args.input_files_or_dirs = [x.expanduser() for x in args.input_files_or_dirs]
    if len(args.input_files_or_dirs) != len(args.corpus_types):
        parser.error(
            f"Number {len(args.input_files_or_dirs)} of input files or directories in parameter "
            f"`--input_files_or_dirs` {args.input_files_or_dirs} is not equal to the number "
            f"{len(args.corpus_types)} of corpus types {args.corpus_types}."
        )
    args.output_dir = args.output_dir.expanduser()
    if args.allowed_punctuation - small.SUPPORTED_BERT_PUNCTUATION:
        logging.warning(
            f"Punctuation marks {args.allowed_punctuation - small.SUPPORTED_BERT_PUNCTUATION} are not allowed for BERT "
            f"labels."
        )
    args.sequence_length_range = tuple(args.sequence_length_range)
    return args


if __name__ == "__main__":
    main()
