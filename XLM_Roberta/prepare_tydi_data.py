# coding=utf-8
# Copyright 2020 The Google Research Team Authors.
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
r"""Converts an TyDi dataset file to tf examples.

Notes:
  Largely follows prepare_nq_data.py from the Natural Questions
  (https://github.com/google-research/language/tree/master/language/question_answering/bert_joint)
  Key difference include:
    * contains language identification.
    * works with byte offset instead of document token.
    * no table or list.
"""

import collections
import gzip
import json
import random

import tensorflow.compat.v1 as tf
import debug
import preproc
import tf_io

flags = tf.flags
FLAGS = flags.FLAGS


flags.DEFINE_string(
    "input_jsonl", None,
    "Gzipped files containing NQ examples in Json format, one per line.")

flags.DEFINE_string("output_tfrecord", None,
                    "Output tf record file with all features extracted.")

flags.DEFINE_string("record_count_file", None,
                    "Output file that will contain a single integer "
                    "(the number of records in `output_tfrecord`). "
                    "This should always be used when generating training data "
                    "as it is required by the training program.")

flags.DEFINE_string("vocab_file", "modified_vocab.txt",
                    "The vocabulary file that the BERT model was trained on.")

flags.DEFINE_bool(
    "is_training", True,
    "Whether to prepare features for training or for evaluation. Eval features "
    "don't include gold labels")

flags.DEFINE_bool(
    "oversample", False,
    "Whether to sample languages with fewer data samples more heavily.")

flags.DEFINE_bool(
    "fail_on_invalid", True,
    "Stop immediately on encountering an invalid example? "
    "If false, just print a warning and skip it.")

flags.DEFINE_integer("max_oversample_ratio", 10,
                     "Maximum number a single example can be oversampled.")

flags.DEFINE_integer(
    "max_examples", 0,
    "If positive, stop once these many examples have been converted.")

flags.DEFINE_integer(
    "max_passages", 45, "Maximum number of passages to consider for a "
    "single article. If an article contains more than"
    "this, they will be discarded during training. "
    "BERT's WordPiece vocabulary must be modified to include "
    "these within the [unused*] vocab IDs.")

flags.DEFINE_integer(
    "max_position", 45,
    "Maximum passage position for which to generate special tokens.")

flags.DEFINE_integer(
    "max_seq_length", 512,
    "The maximum total input sequence length after WordPiece tokenization. "
    "Sequences longer than this will be truncated, and sequences shorter "
    "than this will be padded.")

flags.DEFINE_integer(
    "doc_stride", 128,
    "When splitting up a long document into chunks, how much stride to "
    "take between chunks.")

flags.DEFINE_integer(
    "max_question_length", 64,
    "The maximum number of tokens for the question. Questions longer than "
    "this will be truncated to this length.")

flags.DEFINE_float(
    "include_unknowns", -1.0,
    "If positive, probability of including answers of type `UNKNOWN`.")


def get_lang_counts(input_jsonl_pattern):
  """Gets the number of examples for each language."""
  lang_dict = collections.Counter()
  for input_path in tf.gfile.Glob(input_jsonl_pattern):
    with gzip.GzipFile(fileobj=tf.gfile.Open(input_path)) as input_file:  # pytype: disable=wrong-arg-types
      for line in input_file:
        json_elem = json.loads(line)
        lang_dict[json_elem["language"]] += 1
  return lang_dict


def read_entries(input_jsonl_pattern, fail_on_invalid):
  """Reads TyDi QA examples from JSONL files.

  Args:
    input_jsonl_pattern: Glob of the gzipped JSONL files to read.
    fail_on_invalid: See FLAGS.fail_on_invalid.

  Yields:
    tuple:
      input_file: str
      line_no: int
      tydi_entry: "TyDiEntry"s, dicts as returned by `create_entry_from_json`,
        one per line of the input JSONL files.
      debug_info: Dict containing debugging data.
  """
  for input_path in tf.gfile.Glob(input_jsonl_pattern):
    with gzip.GzipFile(fileobj=tf.gfile.Open(input_path, "rb")) as input_file:  # pytype: disable=wrong-arg-types
      for line_no, line in enumerate(input_file, 1):
        json_elem = json.loads(line, object_pairs_hook=collections.OrderedDict)
        entry = preproc.create_entry_from_json(
            json_elem,
            max_passages=FLAGS.max_passages,
            max_position=FLAGS.max_position,
            fail_on_invalid=fail_on_invalid)

        if not entry:
          tf.logging.info("Invalid Example %d", json_elem["example_id"])
          if FLAGS.fail_on_invalid:
            raise ValueError("Invalid example at {}:{}".format(
                input_path, line_no))

        # Return a `debug_info` dict that methods throughout the codebase
        # append to with debugging information.
        debug_info = {"json": json_elem}
        yield input_path, line_no, entry, debug_info


def main(_):
  examples_processed = 0
  num_examples_with_correct_context = 0
  num_errors = 0
  tf_examples = []
  sample_ratio = {}

  import logging
  log = logging.getLogger('tensorflow')
  log.setLevel(logging.DEBUG)

  formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')

  fh = logging.FileHandler('tensorflow.log')
  fh.setLevel(logging.DEBUG)
  fh.setFormatter(formatter)
  log.addHandler(fh)

  # Print the first 25 examples to let user know what's going on.
  num_examples_to_print = 2500

  if FLAGS.oversample and FLAGS.is_training:
    lang_count = get_lang_counts(FLAGS.input_jsonl)
    max_count = max(count for lang, count in lang_count.items())
    for lang, curr_count in lang_count.items():
      sample_ratio[lang] = int(
          min(FLAGS.max_oversample_ratio, max_count / curr_count))
  creator_fn = tf_io.CreateTFExampleFn(
      is_training=FLAGS.is_training,
      max_question_length=FLAGS.max_question_length,
      max_seq_length=FLAGS.max_seq_length,
      doc_stride=FLAGS.doc_stride,
      include_unknowns=FLAGS.include_unknowns,
      vocab_file=FLAGS.vocab_file)
  reverse_vocab_table = {
      word_id: word for word, word_id in creator_fn.vocab.items()
  }
  tf.logging.info("Reading examples from glob: %s", FLAGS.input_jsonl)
  for filename, line_no, entry, debug_info in read_entries(
      FLAGS.input_jsonl, fail_on_invalid=FLAGS.fail_on_invalid):
    errors = []
    for tf_example in creator_fn.process(entry, errors, debug_info):
      if FLAGS.oversample:
        tf_examples.extend([tf_example] * sample_ratio[entry["language"]])
      else:
        tf_examples.append(tf_example)

    if errors or examples_processed < num_examples_to_print:
      debug.log_debug_info(filename, line_no, entry, debug_info,
                           reverse_vocab_table)

    if examples_processed % 10 == 0:
      tf.logging.info("Examples processed: %d", examples_processed)
    examples_processed += 1

    if errors:
      tf.logging.info(
          "Encountered errors while creating {} example ({}:{}): {}".format(
              entry["language"], filename, line_no, "; ".join(errors)))
      if FLAGS.fail_on_invalid:
        raise ValueError(
            "Encountered errors while creating example ({}:{}): {}".format(
                filename, line_no, "; ".join(errors)))
      num_errors += 1
      if num_errors % 10 == 0:
        tf.logging.info("Errors so far: %d", num_errors)

    if entry["has_correct_context"]:
      num_examples_with_correct_context += 1
    if FLAGS.max_examples > 0 and examples_processed >= FLAGS.max_examples:
      break
  tf.logging.info("Examples with correct context retained: %d of %d",
                  num_examples_with_correct_context, examples_processed)

  # Even though the input is shuffled, we need to do this in case we're
  # oversampling.
  random.shuffle(tf_examples)
  num_features = len(tf_examples)
  tf.logging.info("Number of total features %d", num_features)
  tf.logging.info("'Features' are windowed slices of a document paired with "
                  "a supervision label.")

  with tf.python_io.TFRecordWriter(FLAGS.output_tfrecord) as writer:
    for tf_example in tf_examples:
      writer.write(tf_example.SerializeToString())
  if FLAGS.record_count_file:
    with tf.gfile.Open(FLAGS.record_count_file, "w") as writer:
      writer.write(str(num_features))

if __name__ == "__main__":
  tf.app.run()