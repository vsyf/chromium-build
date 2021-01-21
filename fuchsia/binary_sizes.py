#!/usr/bin/env python
#
# Copyright 2020 The Chromium Authors. All rights reserved.
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.
'''Implements Chrome-Fuchsia package binary size checks.'''

from __future__ import print_function

import argparse
import collections
import copy
import json
import logging
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import traceback
import uuid

from common import GetHostToolPathFromPlatform, GetHostArchFromPlatform
from common import SDK_ROOT, DIR_SOURCE_ROOT

# Structure representing the compressed and uncompressed sizes for a Fuchsia
# package.
PackageSizes = collections.namedtuple('PackageSizes',
                                      ['compressed', 'uncompressed'])


def CreateSizesExternalDiagnostic(sizes_guid):
  """Creates a histogram external sizes diagnostic."""

  benchmark_diagnostic = {
      'type': 'GenericSet',
      'guid': str(sizes_guid),
      'values': ['sizes'],
  }

  return benchmark_diagnostic


def CreateSizesHistogramItem(name, size, sizes_guid):
  """Create a performance dashboard histogram from the histogram template and
  binary size data."""

  # Chromium performance dashboard histogram containing binary size data.
  histogram = {
      'name': name,
      'unit': 'sizeInBytes_smallerIsBetter',
      'diagnostics': {
          'benchmarks': str(sizes_guid),
      },
      'sampleValues': [size],
      'running': [1, size, math.log(size), size, size, size, 0],
      'description': 'chrome-fuchsia package binary sizes',
      'summaryOptions': {
          'avg': True,
          'count': False,
          'max': False,
          'min': False,
          'std': False,
          'sum': False,
      },
  }

  return histogram


def CreateSizesHistogram(package_sizes):
  """Create a performance dashboard histogram from binary size data."""

  sizes_guid = uuid.uuid1()
  histogram = [CreateSizesExternalDiagnostic(sizes_guid)]
  for name, size in package_sizes.items():
    histogram.append(
        CreateSizesHistogramItem('%s_%s' % (name, 'compressed'),
                                 size.compressed, sizes_guid))
    histogram.append(
        CreateSizesHistogramItem('%s_%s' % (name, 'uncompressed'),
                                 size.uncompressed, sizes_guid))
  return histogram


def CreateTestResults(test_status, timestamp):
  """Create test results data to write to JSON test results file.

  The JSON data format is defined in
  https://chromium.googlesource.com/chromium/src/+/master/docs/testing/json_test_results_format.md
  """

  results = {
      'tests': {},
      'interrupted': False,
      'path_delimiter': '.',
      'version': 3,
      'seconds_since_epoch': timestamp,
  }

  num_failures_by_type = {result: 0 for result in ['FAIL', 'PASS', 'CRASH']}
  for metric in test_status:
    actual_status = test_status[metric]
    num_failures_by_type[actual_status] += 1
    results['tests'][metric] = {
        'expected': 'PASS',
        'actual': actual_status,
    }
  results['num_failures_by_type'] = num_failures_by_type

  return results


def GetTestStatus(package_sizes, sizes_config, test_completed):
  """Checks package sizes against size limits.

  Returns a tuple of overall test pass/fail status and a dictionary mapping size
  limit checks to PASS/FAIL/CRASH status."""

  if not test_completed:
    test_status = {'binary_sizes': 'CRASH'}
  else:
    test_status = {}
    for metric, limit in sizes_config['size_limits'].items():
      # Strip the "_compressed" suffix from |metric| if it exists.
      match = re.match(r'(?P<name>\w+)_compressed', metric)
      package_name = match.group('name') if match else metric
      if package_name not in package_sizes:
        raise Exception('package "%s" not in sizes "%s"' %
                        (package_name, str(package_sizes)))
      if package_sizes[package_name].compressed <= limit:
        test_status[metric] = 'PASS'
      else:
        test_status[metric] = 'FAIL'

  all_tests_passed = all(status == 'PASS' for status in test_status.values())

  return all_tests_passed, test_status


def WriteSimpleTestResults(results_path, test_completed):
  """Writes simplified test results file.

  Used when test status is not available.
  """

  simple_isolated_script_output = {
      'valid': test_completed,
      'failures': [],
      'version': 'simplified',
  }
  with open(results_path, 'w') as output_file:
    json.dump(simple_isolated_script_output, output_file)


def WriteTestResults(results_path, test_completed, test_status, timestamp):
  """Writes test results file containing test PASS/FAIL/CRASH statuses."""

  if test_status:
    test_results = CreateTestResults(test_status, timestamp)
    with open(results_path, 'w') as results_file:
      json.dump(test_results, results_file)
  else:
    WriteSimpleTestResults(results_path, test_completed)

  test_results = CreateTestResults(test_status, timestamp)
  with open(results_path, 'w') as results_file:
    json.dump(test_results, results_file)


def GetZstdPathFromPlatform():
  """Returns path to zstd compression utility based on the current platform."""

  arch = GetHostArchFromPlatform()
  if arch == 'arm64':
    zstd_arch_dir = 'zstd-linux-arm64'
  elif arch == 'x64':
    zstd_arch_dir = 'zstd-linux-x64'
  else:
    raise Exception('zstd path unknown for architecture "%s"' % arch)

  return os.path.join(DIR_SOURCE_ROOT, 'third_party', zstd_arch_dir, 'bin',
                      'zstd')


def CompressedSize(file_path, compression_args):
  """Calculates size file after zstd compression.  Uses non-chunked compression
  (Fuchsia uses chunked compression which is not available in the zstd command
  line tool).  The compression level can be set using compression_args."""

  zstd_path = GetZstdPathFromPlatform()
  devnull = open(os.devnull)
  proc = subprocess.Popen([zstd_path, '-f', file_path, '-c'] + compression_args,
                          stdout=open(os.devnull, 'w'),
                          stderr=subprocess.PIPE)
  proc.wait()
  zstd_stats = proc.stderr.readline()

  # Match a compressed bytes total from zstd stderr output like
  # test                 : 14.04%   (  3890 =>    546 bytes, test.zst)
  zstd_compressed_bytes_re = r'\d+\s+=>\s+(?P<bytes>\d+) bytes,'

  match = re.search(zstd_compressed_bytes_re, zstd_stats)
  if not match:
    print(zstd_stats)
    raise Exception('Could not get compressed bytes for %s' % file_path)

  return int(match.group('bytes'))


def ExtractFarFile(file_path, extract_dir):
  """Extracts contents of a Fuchsia archive file to the specified directory."""

  far_tool = GetHostToolPathFromPlatform('far')

  if not os.path.isfile(far_tool):
    raise Exception('Could not find FAR host tool "%s".' % far_tool)
  if not os.path.isfile(file_path):
    raise Exception('Could not find FAR file "%s".' % file_path)
  if os.path.isdir(extract_dir):
    raise Exception('Could not find extraction directory "%s".' % extract_dir)

  subprocess.check_call([
      far_tool, 'extract',
      '--archive=%s' % file_path,
      '--output=%s' % extract_dir
  ])


def GetBlobNameHashes(meta_dir):
  """Returns mapping from Fuchsia pkgfs paths to blob hashes.  The mapping is
  read from the extracted meta.far archive contained in an extracted package
  archive."""

  blob_name_hashes = {}
  contents_path = os.path.join(meta_dir, 'meta', 'contents')
  with open(contents_path) as lines:
    for line in lines:
      (pkgfs_path, blob_hash) = line.strip().split('=')
      blob_name_hashes[pkgfs_path] = blob_hash
  return blob_name_hashes


def CommitPositionFromBuildProperty(value):
  """Extracts the chromium commit position from a builders got_revision_cp
  property."""

  # Match a commit position from a build properties commit string like
  # "refs/heads/master@{#819458}"
  test_arg_commit_position_re = r'\{#(?P<position>\d+)\}'

  match = re.search(test_arg_commit_position_re, value)
  if match:
    return int(match.group('position'))
  raise RuntimeError('Could not get chromium commit position from test arg.')


# Compiled regular expression matching strings like *.so, *.so.1, *.so.2, ...
SO_FILENAME_REGEXP = re.compile(r'\.so(\.\d+)?$')


def GetSdkModulesForExclusion():
  """Finds shared objects (.so) under the Fuchsia SDK arch directory in dist or
  lib subdirectories.

  Returns a set of shared objects' filenames.
  """

  # Fuchsia SDK arch directory path (contains all shared object files).
  sdk_arch_dir = os.path.join(SDK_ROOT, 'arch')
  # Leaf subdirectories containing shared object files.
  sdk_so_leaf_dirs = ['dist', 'lib']
  # Match a shared object file name.
  sdk_so_filename_re = r'\.so(\.\d+)?$'

  lib_names = set()
  for dirpath, _, file_names in os.walk(sdk_arch_dir):
    if os.path.basename(dirpath) in sdk_so_leaf_dirs:
      for name in file_names:
        if SO_FILENAME_REGEXP.search(name):
          lib_names.add(name)
  return lib_names


def FarBaseName(name):
  _, name = os.path.split(name)
  name = re.sub(r'\.far$', '', name)
  return name


def GetBlobSizes(far_file, build_out_dir, extract_dir, compression_args):
  """Calculates compressed and uncompressed blob sizes for specified FAR file.
  Does not count blobs from SDK libraries."""

  #TODO(crbug.com/1126177): Use partial sizes for blobs shared by packages.
  base_name = FarBaseName(far_file)

  # Extract files and blobs from the specified Fuchsia archive.
  far_file_path = os.path.join(build_out_dir, far_file)
  far_extract_dir = os.path.join(extract_dir, base_name)
  ExtractFarFile(far_file_path, far_extract_dir)

  # Extract the meta.far archive contained in the specified Fuchsia archive.
  meta_far_file_path = os.path.join(far_extract_dir, 'meta.far')
  meta_far_extract_dir = os.path.join(extract_dir, '%s_meta' % base_name)
  ExtractFarFile(meta_far_file_path, meta_far_extract_dir)

  # Map Linux filesystem blob names to blob hashes.
  blob_name_hashes = GetBlobNameHashes(meta_far_extract_dir)

  # File names whose sizes are not charged against component's size budgets.
  # Fuchsia SDK modules and the ICU icudtl.dat file are excluded from sizes.
  excluded_files = GetSdkModulesForExclusion() | set(['icudtl.dat'])

  # Sum compresses and uncompressed blob sizes, except for SDK blobs.
  blob_sizes = {}
  for blob_name in blob_name_hashes:
    if os.path.basename(blob_name) not in excluded_files:
      blob_path = os.path.join(far_extract_dir, blob_name_hashes[blob_name])
      compressed_size = CompressedSize(blob_path, compression_args)
      uncompressed_size = os.path.getsize(blob_path)
      blob_sizes[blob_name] = PackageSizes(compressed_size, uncompressed_size)

  return blob_sizes


def GetPackageSizes(far_files, build_out_dir, extract_dir, compression_args,
                    print_sizes):
  """Calculates compressed and uncompressed package sizes from blob sizes.
  Does not count blobs from SDK libraries."""

  #TODO(crbug.com/1126177): Use partial sizes for blobs shared by
  # non Chrome-Fuchsia packages.

  # Get sizes for blobs contained in packages.
  package_blob_sizes = {}
  for far_file in far_files:
    package_name = FarBaseName(far_file)
    package_blob_sizes[package_name] = GetBlobSizes(far_file, build_out_dir,
                                                    extract_dir,
                                                    compression_args)

  # Optionally print package blob sizes (does not count sharing).
  if print_sizes:
    for package_name in sorted(package_blob_sizes.keys()):
      print('Package: %s' % package_name)
      for blob_name in sorted(package_blob_sizes[package_name].keys()):
        size = package_blob_sizes[package_name][blob_name]
        print('blob: %s %d %d' %
              (blob_name, size.compressed, size.uncompressed))

  # Count number of packages sharing blobs (a count of 1 is not shared).
  blob_counts = collections.defaultdict(int)
  for package_name in package_blob_sizes:
    for blob_name in package_blob_sizes[package_name]:
      blob_counts[blob_name] += 1

  # Package sizes are the sum of blob sizes divided by their share counts.
  package_sizes = {}
  for package_name in package_blob_sizes:
    compressed_size = 0
    uncompressed_size = 0
    for blob_name in package_blob_sizes[package_name]:
      count = blob_counts[blob_name]
      size = package_blob_sizes[package_name][blob_name]
      compressed_size += size.compressed / count
      uncompressed_size += size.uncompressed / count
    package_sizes[package_name] = PackageSizes(compressed_size,
                                               uncompressed_size)

  return package_sizes


def GetBinarySizes(args, sizes_config):
  """Get binary size data for packages specified in args.

  If "total_size_name" is set, then computes a synthetic package size which is
  the aggregated sizes across all blobs."""

  # Calculate compressed and uncompressed package sizes.
  extract_dir = args.extract_dir if args.extract_dir else tempfile.mkdtemp()
  package_sizes = GetPackageSizes(sizes_config['far_files'], args.build_out_dir,
                                  extract_dir, sizes_config['zstd_args'],
                                  args.verbose)
  if not args.extract_dir:
    shutil.rmtree(extract_dir)

  # Optionally calculate total compressed and uncompressed package sizes.
  if 'far_total_name' in sizes_config:
    compressed = sum([a.compressed for a in package_sizes.values()])
    uncompressed = sum([a.uncompressed for a in package_sizes.values()])
    package_sizes[sizes_config['far_total_name']] = PackageSizes(
        compressed, uncompressed)

  for name, size in package_sizes.items():
    print('%s: compressed %d, uncompressed %d' %
          (name, size.compressed, size.uncompressed))

  return package_sizes


def main():
  parser = argparse.ArgumentParser()
  parser.add_argument(
      '--build-out-dir',
      '--output-directory',
      type=os.path.realpath,
      required=True,
      help='Location of the build artifacts.',
  )
  parser.add_argument(
      '--extract-dir',
      help='Debugging option, specifies directory for extracted FAR files.'
      'If present, extracted files will not be deleted after use.')
  parser.add_argument(
      '--isolated-script-test-output',
      type=os.path.realpath,
      help='File to which simplified JSON results will be written.')
  parser.add_argument(
      '--output-dir',
      help='Optional directory for histogram output file.  This argument is '
      'automatically supplied by the recipe infrastructure when this script '
      'is invoked by a recipe call to api.chromium.runtest().')
  parser.add_argument(
      '--sizes-path',
      default=os.path.join('fuchsia', 'release', 'size_tests',
                           'fyi_sizes.json'),
      help='path to package size limits json file.  The path is relative to '
      'the workspace src directory')
  parser.add_argument(
      '--test-revision-cp',
      help='Set the chromium commit point NNNNNN from a build property value '
      'like "refs/heads/master@{#NNNNNNN}".  Intended for use in recipes with '
      'the build property got_revision_cp',
  )
  parser.add_argument('--verbose',
                      '-v',
                      action='store_true',
                      help='Enable verbose output')
  # Accepted to conform to the isolated script interface, but ignored.
  parser.add_argument('--isolated-script-test-filter', help=argparse.SUPPRESS)
  parser.add_argument('--isolated-script-test-perf-output',
                      help=argparse.SUPPRESS)
  args = parser.parse_args()

  if args.verbose:
    print('Fuchsia binary sizes')
    print('Working directory', os.getcwd())
    print('Args:')
    for var in vars(args):
      print('  {}: {}'.format(var, getattr(args, var) or ''))

  # Optionally prefix the output_dir to the histogram_path.
  if args.output_dir and args.histogram_path:
    args.histogram_path = os.path.join(args.output_dir, args.histogram_path)

  if not os.path.isdir(args.build_out_dir):
    raise Exception('Could not find build output directory "%s".' %
                    args.build_out_dir)

  if args.extract_dir and not os.path.isdir(args.extract_dir):
    raise Exception(
        'Could not find FAR file extraction output directory "%s".' %
        args.extract_dir)

  with open(os.path.join(DIR_SOURCE_ROOT, args.sizes_path)) as sizes_file:
    sizes_config = json.load(sizes_file)

  if args.verbose:
    print('Sizes Config:')
    print(json.dumps(sizes_config))

  # If the zstd compression level is not specified, use Fuchsia's default level.
  sizes_config.setdefault('zstd_args', [])
  if not any(re.match(r'-\d+$', arg) for arg in sizes_config['zstd_args']):
    sizes_config['zstd_args'].append('-14')

  for far_rel_path in sizes_config['far_files']:
    far_abs_path = os.path.join(args.build_out_dir, far_rel_path)
    if not os.path.isfile(far_abs_path):
      raise Exception('Could not find FAR file "%s".' % far_abs_path)

  test_name = 'sizes'
  timestamp = time.time()
  test_completed = False
  all_tests_passed = False
  test_status = {}
  sizes_histogram = []

  results_directory = None
  if args.isolated_script_test_output:
    results_directory = os.path.join(
        os.path.dirname(args.isolated_script_test_output), test_name)
    if not os.path.exists(results_directory):
      os.makedirs(results_directory)

  try:
    package_sizes = GetBinarySizes(args, sizes_config)
    sizes_histogram = CreateSizesHistogram(package_sizes)
    test_completed = True
  except:
    _, value, trace = sys.exc_info()
    traceback.print_tb(trace)
    print(str(value))
  finally:
    if test_completed:
      all_tests_passed, test_status = GetTestStatus(package_sizes, sizes_config,
                                                    test_completed)

    if results_directory:
      WriteTestResults(os.path.join(results_directory, 'test_results.json'),
                       test_completed, test_status, timestamp)
      with open(os.path.join(results_directory, 'perf_results.json'), 'w') as f:
        json.dump(sizes_histogram, f)

    if args.isolated_script_test_output:
      WriteTestResults(args.isolated_script_test_output, test_completed,
                       test_status, timestamp)

    return 0 if all_tests_passed else 1


if __name__ == '__main__':
  sys.exit(main())
