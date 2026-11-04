"""Download full-protocol and ad-hoc ABAP detections (Python 3.9+, standard library)."""

import argparse
import calendar
import csv
import gzip
import io
import json
import shutil
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.request import urlopen
from zoneinfo import ZoneInfo


# Settings ----------------------------------------------------------------
API = "https://api.birdmap.africa/sabap2/v2"
PAGE_SIZE = 25_000
DATA_DIR = Path(__file__).resolve().parents[2] / "data/raw/abap/downloaded"
FIELDS = [
    "CardNo", "StartDate", "EndDate", "StartTime", "Pentad", "ObserverNo",
    "ObserverName", "TotalHours", "Hour1", "Hour2", "Hour3", "Hour4", "Hour5",
    "Hour6", "Hour7", "Hour8", "Hour9", "Hour10", "TotalSpp", "InclNight",
    "AllHabitats", "Spp", "Common_group", "Common_species", "Genus", "Species",
    "Sequence",
    "Protocol", "Common_name", "Taxonomic_name",
]


def download(url, destination):
    """Archive responses and reuse completed files when resuming a snapshot."""
    if destination.exists():
        with gzip.open(destination, "rb") as source:
            return source.read()
    with urlopen(url, timeout=120) as response:
        content = response.read()
    with gzip.open(destination.with_suffix(".tmp"), "wb") as output:
        output.write(content)
    destination.with_suffix(".tmp").replace(destination)
    return content


def download_csv(url, destination):
    """Read BirdMap CSV responses without converting identifiers to numbers."""
    return list(csv.DictReader(io.StringIO(download(url, destination).decode("utf-8-sig"))))


if __name__ == "__main__":
    # Select the observation period ----------------------------------------
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--country", default="kenya", help="BirdMap country identifier (default: kenya).")
    parser.add_argument("--refresh", action="store_true", help="Download the query again, replacing its cached responses.")
    parser.add_argument("--start-date", type=date.fromisoformat, help="Inclusive checklist start date (default: earliest available checklist).")
    parser.add_argument("--all-history", action="store_true", help="Start at the earliest checklist in the country card catalogue.")
    parser.add_argument("--end-date", type=date.fromisoformat, default=datetime.now(ZoneInfo("Africa/Nairobi")).date(), help="Inclusive checklist start date (default: today in Kenya).")
    parser.add_argument("--output-dir", type=Path, default=DATA_DIR, help="Parent directory for country-and-date folders.")
    parser.add_argument("--resume", type=Path, help="Resume an existing snapshot using its saved dates and responses.")
    parser.add_argument("--workers", type=int, default=4, help="Concurrent ad-hoc species requests (default: 4).")
    args = parser.parse_args()
    args.start_date_was_supplied = args.start_date is not None

    # Identify the query and reuse its saved responses -----------------------
    started = datetime.now(timezone.utc)
    args.country = args.country.lower().replace(" ", "")
    if args.country == "all":
        parser.error("All-region downloads are not supported: the API did not return an unfiltered response.")
    region = f"country/{args.country}"
    if args.resume:
        snapshot = args.resume
        request = json.loads((snapshot / "request.json").read_text())
        args.start_date = date.fromisoformat(request["start_date"])
        args.end_date = date.fromisoformat(request["end_date"])
        args.country = request.get("country", "kenya")
        region = f"country/{args.country}"
    else:
        if args.all_history or args.start_date is None:
            cards_url = f"{API}/coverage/{region}/cards?format=csv"
            with urlopen(cards_url, timeout=120) as response:
                cards_content = response.read()
            cards = list(csv.DictReader(io.StringIO(cards_content.decode("utf-8-sig"))))
            args.start_date = date.fromisoformat(min(row["StartDate"] for row in cards))
        snapshot = args.output_dir / f"{args.country}_{args.start_date}_{args.end_date}"
        if args.refresh and snapshot.exists():
            shutil.rmtree(snapshot)
        snapshot.mkdir(parents=True, exist_ok=True)
        if (snapshot / "manifest.json").exists():
            print(f"Completed download already exists: {snapshot}", flush=True)
            raise SystemExit(0)
        if (snapshot / "request.json").exists():
            request = json.loads((snapshot / "request.json").read_text())
        else:
            request = {"country": args.country, "start_date": str(args.start_date), "end_date": str(args.end_date), "started_at": started.isoformat()}
            (snapshot / "request.json").write_text(json.dumps(request, indent=2) + "\n")
        if args.all_history or not args.start_date_was_supplied:
            with gzip.open(snapshot / "cards.csv.gz", "wb") as output:
                output.write(cards_content)
    snapshot.mkdir(parents=True, exist_ok=True)
    manifest = {
        "source": "BirdMap",
        "country": args.country,
        "api": API,
        "started_at": request["started_at"],
        "start_date": args.start_date.isoformat(),
        "end_date": args.end_date.isoformat(),
        "date_basis": "checklist StartDate, inclusive",
        "protocol_coverage": ["full_protocol", "adhoc"],
        "record_selection": "API-exposed usable detections; non-detections excluded",
        "incidental_coverage": "Standalone incidental records are not included",
        "taxonomy": "Original ABAP Spp codes and names; not yet mapped to eBird taxonomy",
        "months": [],
        "adhoc_species": [],
    }
    print(f"Snapshot: {snapshot}", flush=True)
    (snapshot / "manifest.json").unlink(missing_ok=True)

    # Download monthly partitions and paginate within each month -----------
    with (snapshot / "records.csv").open("w", newline="", encoding="utf-8") as output:
        writer = csv.DictWriter(output, fieldnames=FIELDS)
        writer.writeheader()
        start = args.start_date
        while start <= args.end_date:
            end = min(date(start.year, start.month, calendar.monthrange(start.year, start.month)[1]), args.end_date)
            base = f"{API}/R/{start}/{end}/{region}"
            prefix = f"{start}_{end}"
            count = json.loads(download(f"{base}/count", snapshot / f"{prefix}_count.json.gz"))
            expected = int(count["data"][0]["records"])
            month = {"start_date": str(start), "end_date": str(end), "expected_records": expected, "downloaded_records": 0, "urls": [f"{base}/count"]}

            for first in range(0, expected, PAGE_SIZE):
                url = f"{base}/data?first={first}"
                page = json.loads(download(url, snapshot / f"{prefix}_{first:07d}.json.gz"))
                writer.writerows({**row, "Protocol": "full_protocol"} for row in page["data"])
                month["downloaded_records"] += len(page["data"])
                month["urls"].append(url)

            # Do not mark a truncated or changing response as a complete snapshot.
            if month["downloaded_records"] != expected:
                raise RuntimeError(f"{prefix}: expected {expected} records, downloaded {month['downloaded_records']}. Re-run to obtain a fresh snapshot.")
            manifest["months"].append(month)
            print(f"{start} to {end}: {expected:,} records", flush=True)
            start = end + timedelta(days=1)

        # Download ad-hoc detections for every species in the country catalogue.
        species_url = f"{API}/coverage/{region}/species?format=csv"
        species = download_csv(species_url, snapshot / "species.csv.gz")
        species = [row for row in species if int(row["adn"]) > 0]
        years = ",".join(str(year) for year in range(args.start_date.year, args.end_date.year + 1))
        urls = [f"{API}/cards/species/0/{row['Ref']}/{region}?format=csv&inclnull=0&year={years}&adhoc=1" for row in species]
        (snapshot / "adhoc").mkdir(exist_ok=True)
        destinations = [snapshot / "adhoc" / f"{row['Ref']}.csv.gz" for row in species]
        manifest["species_catalogue_url"] = species_url
        print(f"Downloading ad-hoc records for {len(species):,} species", flush=True)
        with ThreadPoolExecutor(max_workers=args.workers) as pool:
            for index, (taxon, url, rows) in enumerate(zip(species, urls, pool.map(download_csv, urls, destinations)), 1):
                rows = [row for row in rows if str(args.start_date) <= row["StartDate"] <= str(args.end_date) and row["Spp"] != "-"]
                writer.writerows({**row, "Protocol": "adhoc", **{key: taxon[key] for key in ["Common_group", "Common_species", "Genus", "Species"]}} for row in rows)
                manifest["adhoc_species"].append({"Spp": taxon["Ref"], "records": len(rows), "url": url})
                if index % 50 == 0 or index == len(species):
                    print(f"Ad-hoc: {index:,}/{len(species):,} species downloaded", flush=True)

    # Write the completion manifest only after all partitions succeed -------
    manifest["records_by_protocol"] = {
        "full_protocol": sum(month["downloaded_records"] for month in manifest["months"]),
        "adhoc": sum(taxon["records"] for taxon in manifest["adhoc_species"]),
    }
    manifest["records"] = sum(manifest["records_by_protocol"].values())
    manifest["completed_at"] = datetime.now(timezone.utc).isoformat()
    (snapshot / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    print(f"Saved {manifest['records']:,} records to {snapshot / 'records.csv'}", flush=True)
