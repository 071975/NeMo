import argparse
import io
import logging
import multiprocessing as mp
import random
import re
from itertools import accumulate, chain
from pathlib import Path
from queue import Empty
from subprocess import run
from time import sleep
from typing import List, Tuple

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


SUPPORTED_CORPUS_TYPES = ["wikipedia", "europarl", "TED", "rapid", "news-commentary"]


FORBIDDEN_PUNCTUATION_IN_THE_START_OF_SEGMENT = re.compile(f'^[^{WC}]+')


MAX_NUM_CHARACTERS_IN_1_FILE = 10 ** 9
BUFFER_SIZE = 2 ** 24
REPORT_PROGRESS_PERIOD = 5000


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


def count_lines_in_file(file_path, start=0, num_characters=None):
    with file_path.open() as f:
        f.seek(start)
        count = sum(count_in_blocks(f, specific_to_count='\n', num_characters=num_characters))
    return count


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
    progress_process = mp.Process(target=big.show_prog, args=(progress_queue, count_lines_in_file(file_path), "Lines"))
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
):
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
):
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
):
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
):
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
):
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


def get_segment_info(sentences: List[str], sequence_length_range: Tuple[int, int], num_segments: int, file: Path):
    num_words = 0
    sent_i = len(sentences) - 1
    while sent_i > 0 and num_words < sequence_length_range[1]:
        num_words += count_words(sentences[sent_i])
        sent_i -= 1
    if num_segments > sent_i + 1:
        raise ValueError(
            f"Not enough words ({num_words}) in file {file} to cut {num_segments} segments with number of words in "
            f"range {sequence_length_range}"
        )
    logging.info(f"Cutting {num_segments} segments from {sent_i + 1} sentences in file {file}.")
    start_sentences = sorted(random.sample(list(range(sent_i + 1)), num_segments))
    lengths = list(range(sequence_length_range[0], sequence_length_range[1]))
    num_words = []
    for i in range(num_segments):
        num_words.append(lengths[i % len(lengths)])
    return start_sentences, num_words


def cut_segment(text, shift, num_words_in_segment):
    segment = ""
    for word_i, m in enumerate(small.WORD_WITH_PRECEDING_AND_FOLLOWING_PUNCTUATION.finditer(text)):
        if word_i < shift:
            continue
        if word_i < num_words_in_segment + shift:
            break
        segment += m.group(0)
    return segment


def extract_dev_text_segments_worker(
    file: Path,
    num_segments: int,
    sequence_length_range: Tuple[int, int],
    after_extraction_document_dir: Path,
    progress_queue: mp.Queue,
):
    after_extraction_document_dir.mkdir(parents=True, exist_ok=True)
    output_file = after_extraction_document_dir / file.name
    segments = []
    docs = big.read_docs_from_file(file)[0]
    sentences = list(chain(*[doc['text'].splitlines() for doc in docs.values()]))
    start_sentences, num_words_by_segments = get_segment_info(sentences, sequence_length_range, num_segments, file)
    curr_segment_i = 0
    sentence_i = 0
    progress = 0
    excluded = set()
    with output_file.open('w') as f:
        while sentence_i < len(sentences):
            if sentence_i == start_sentences[curr_segment_i]:
                num_words = count_words(sentences[start_sentences[curr_segment_i]])
                shift = random.randint(0, num_words // 2)
                num_words_raw = 0
                num_sentences_for_segment = 0
                while num_words_raw < shift + num_words_by_segments[curr_segment_i]:
                    num_words_raw += count_words(sentences[sentence_i + num_sentences_for_segment])
                    num_sentences_for_segment += 1
                segments.append(
                    cut_segment(
                        ' '.join(sentences[sentence_i : sentence_i + num_sentences_for_segment]),
                        shift,
                        num_words_by_segments[curr_segment_i],
                    )
                )
                excluded.update({sentence_i + i for i in range(num_sentences_for_segment)})
                curr_segment_i += 1
                progress += 1
                if progress >= 100:
                    progress_queue.put(progress)
                    progress = 0
                sentence_i += 1
            else:
                if sentence_i not in excluded:
                    f.write(sentences[sentence_i] + '\n')
                sentence_i += 1
    assert len(segments) == num_segments, f"{len(segments)} were cut whereas {num_segments} segments were expected."
    progress_queue.put(progress)
    return segments


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
    num_segments_by_files = get_how_many_segments_to_cut_by_files(files, dev_size + test_size)
    num_jobs = min(num_jobs, len(files))
    with Progress(dev_size + test_size, 'Cutting segments', 'segment') as progress_queues:
        with mp.Pool(num_jobs) as pool:
            result = pool.starmap(
                extract_dev_text_segments_worker,
                zip(
                    files,
                    num_segments_by_files,
                    [sequence_length_range] * len(files),
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
    shift = random.randint(0, max(num_words_in_segments))
    # print("permutation:", permutation)
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


def cut_and_save(rank, progress_queue, files, num_passes_through_dataset, output_dir, sequence_range):
    num_words_in_segments = list(range(sequence_range[0], sequence_range[1]))
    for f_i, f in enumerate(files):
        out_file = output_dir / (f.stem + '.txt')
        text = list(big.read_docs_from_file(f)[0].items())
        random.shuffle(text)
        text = '\n'.join([doc[1]['text'] for doc in text])
        text = small.SPACE_DUP.sub(' ', text.replace('\n', ' '))
        with out_file.open('w', buffering=BUFFER_SIZE) as out_f:
            for _ in range(num_passes_through_dataset):
                cut_and_save_one_pass(text, out_f, progress_queue, num_words_in_segments)


def get_how_many_segments_to_cut_by_files(files, size):
    stats = [f.stat().st_size for f in files]
    total_size = sum(stats)
    fracs = [s / total_size for s in stats]
    sizes = [round(f * size) for f in fracs[:-1]]
    sizes += [size - sum(sizes)]
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
    num_files_per_job = len(files) // num_jobs
    distributed_files = (
        [files[i * num_files_per_job: (i + 1) * num_files_per_job] for i in range(num_jobs - 1)]
        + [files[(num_jobs - 1) * num_files_per_job:]]
    )
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
                    range(num_jobs),
                    [progress_queue] * num_jobs,
                    distributed_files,
                    [num_passes_through_dataset] * num_jobs,
                    [output_dir] * num_jobs,
                    [sequence_length_range] * num_jobs,
                )
            )
        )
    progress_queue.put(-1)
    progress_process.join()
    with sorted_text_file.open('w') as out_f:
        run(
            [f'cat'] + [str(p.resolve()) for p in output_dir.iterdir() if is_int(p.stem) and p.suffixes == ['.txt']],
            stdout=out_f,
        )


def shuffle_file_lines(input_file, output_file):
    with output_file.open('w') as f:
        run(['shuf', str(input_file)], stdout=f)


def join_sentence_len(di_ss_se, sentence_len_by_docs):
    return sum(sentence_len_by_docs[di_ss_se[0]][di_ss_se[1]: di_ss_se[2]])


def main():
    args = get_args(SUPPORTED_CORPUS_TYPES, add_resume_argument=True)
    document_dir = args.output_dir / Path("documents")
    if args.resume_from is None:
        tokenizer = get_tokenizer(args.tokenizer)
        doc_id_to_file_i = {}
        start_doc_id, start_file_id = 0, 0
        for corpus_type, file_path in zip(args.corpus_types, args.input_files):
            if corpus_type == SUPPORTED_CORPUS_TYPES[0]:  # wikipedia
                logging.info(f"Preprocessing wikipedia file {file_path}...")
                corpus_doc_id_to_file_i = preprocess_wikipedia_parallel(
                    args.num_jobs,
                    file_path,
                    document_dir,
                    args.input_language,
                    tokenizer,
                    start_doc_id,
                    start_file_id,
                    args.nltk_tokenization,
                )
            elif corpus_type == SUPPORTED_CORPUS_TYPES[1]:  # europarl
                corpus_doc_id_to_file_i = preprocess_europarl(
                    file_path, document_dir, args.input_language, start_doc_id, start_file_id, tokenizer,
                )
            elif corpus_type == SUPPORTED_CORPUS_TYPES[2]:  # TED
                corpus_doc_id_to_file_i = preprocess_ted(
                    file_path, document_dir, args.input_language, start_doc_id, start_file_id, tokenizer,
                )
            elif corpus_type == SUPPORTED_CORPUS_TYPES[3]:  # rapid
                corpus_doc_id_to_file_i = preprocess_rapid(
                    file_path, document_dir, args.input_language, start_doc_id, start_file_id, tokenizer,
                )
            elif corpus_type == SUPPORTED_CORPUS_TYPES[4]:  # news-commentary
                corpus_doc_id_to_file_i = preprocess_news_commentary(
                    file_path, document_dir, args.input_language, start_doc_id, start_file_id, tokenizer,
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
    sorted_text_file = args.output_dir / 'sorted_text.txt'
    if args.resume_from is None or args.resume_from in ["cutting"]:
        rp = '(' not in args.allowed_punctuation or ')' not in args.allowed_punctuation
        if rp:
            rp_dir = after_extraction_document_dir.parent / 'documents_without_parentheses'
            remove_parentheses_parallel(after_extraction_document_dir, rp_dir, args.num_jobs)
        else:
            rp_dir = None
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
    size = count_lines_in_file(sorted_text_file)
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
        [args.test_size + args.dev_size, size],
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
        "--input_files",
        help="List of files with input data. You should also provide `--corpus_types` list which elements are types "
        "corresponding files.",
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
        "dataset. Number and order of elements in this list should be equal to the number of elements in `input_files` "
        "list.",
        choices=supported_corpus_types,
        nargs="+",
        required=True,
    )
    parser.add_argument(
        "--num_passes_through_dataset",
        "-S",
        type=int,
        help="How many times the script goes through data to cut train segments. Dev and test are cut train and "
        "sentences used for dev and test are excluded from the process.",
        default=1,
    )
    parser.add_argument("--dev_size", "-d", help="Number of sequences in dev data.", type=int, default=10 ** 4)
    parser.add_argument("--test_size", "-t", help="Percentage of test data.", type=int, default=10 ** 4)
    parser.add_argument(
        "--sequence_length_range",
        "-r",
        help="Minimum and maximum number words in model input sequences. Number of words is sampled "
        "using uniform distribution.",
        type=int,
        nargs=2,
        default=(2, 64),
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
    args.input_files = [x.expanduser() for x in args.input_files]
    if len(args.input_files) != len(args.corpus_types):
        raise ValueError(
            f"Number {len(args.input_files)} of input files {args.input_files} is not equal to the number "
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
