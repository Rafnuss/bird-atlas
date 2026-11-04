"""Download a specified African EBD release."""
import argparse
from pathlib import Path
from bird_atlas import download_EBD

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--year', required=True)
parser.add_argument('--month', required=True)
parser.add_argument('--output', required=True, type=Path)
args = parser.parse_args()
download_EBD(args.year, args.month, output_dir=args.output)
