"""Trim an entire local EBD release once, without selecting taxa or candidate cards."""

import argparse
from pathlib import Path

from bird_atlas.ebd_cache import prepare_ebd_cache

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--input', required=True, type=Path)
parser.add_argument('--cache', required=True, type=Path)
parser.add_argument('--memory-limit', default='1GB')
parser.add_argument('--threads', type=int, default=4)
args = parser.parse_args()
prepare_ebd_cache(args.input, args.cache, memory_limit=args.memory_limit, threads=args.threads)
