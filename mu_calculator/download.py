"""Download only year-2000 UN-adjusted GRUMP country rasters."""

import argparse
import json
import os
from pathlib import Path
import shutil
import zipfile
import requests

MANIFEST = json.loads(Path(__file__).with_name("granules.json").read_text())


class EarthdataSession(requests.Session):
    def rebuild_auth(self, prepared, response):
        # Credentials must never be forwarded to a data host.
        from urllib.parse import urlparse

        if urlparse(prepared.url).hostname == "urs.earthdata.nasa.gov":
            prepared.headers["Authorization"] = requests.auth._basic_auth_str(
                *self.auth
            )
        else:
            prepared.headers.pop("Authorization", None)


def complete(folder, item):
    return all(
        (folder / "bil" / name).is_file()
        and (folder / "bil" / name).stat().st_size == size
        for name, size in item["files"].items()
    )


def ensure_data(folder, countries=None):
    folder = Path(folder)
    selected = [
        item for item in MANIFEST if not countries or item["filename"][:3] in countries
    ]
    if countries and set(countries) - {item["filename"][:3] for item in selected}:
        raise RuntimeError("Unknown country code.")
    (folder / "bil").mkdir(parents=True, exist_ok=True)
    for item in selected:
        if complete(folder, item):
            continue
        destination = folder / item["filename"]
        if not zipfile.is_zipfile(destination):
            username = os.environ.get("EARTHDATA_USERNAME")
            password = os.environ.get("EARTHDATA_PASSWORD")
            if not username or not password:
                raise RuntimeError(
                    "Set EARTHDATA_USERNAME and EARTHDATA_PASSWORD to download missing rasters."
                )
            with EarthdataSession() as session:
                session.auth = (username, password)
                with session.get(
                    item["url"], stream=True, timeout=(30, 120)
                ) as response:
                    if (
                        response.status_code != 200
                        or "html" in response.headers.get("Content-Type", "").lower()
                    ):
                        raise RuntimeError(
                            "Earthdata download denied. Check credentials and approve the SEDAC application in your Earthdata account (Applications > Authorized Apps)."
                        )
                    partial = destination.with_suffix(".zip.part")
                    with partial.open("wb") as output:
                        for chunk in response.iter_content(1024 * 1024):
                            output.write(chunk)
            if not zipfile.is_zipfile(partial):
                partial.unlink(missing_ok=True)
                raise RuntimeError(
                    "Earthdata did not return a ZIP. Check credentials and SEDAC application approval."
                )
            partial.replace(destination)
        try:
            with zipfile.ZipFile(destination) as archive:
                for member in archive.namelist():
                    name = Path(member).name.lower()
                    if name.endswith(("up00ag.bil", "up00ag.hdr")):
                        target = folder / "bil" / name
                        temporary = target.with_suffix(target.suffix + ".part")
                        with (
                            archive.open(member) as source,
                            temporary.open("wb") as output,
                        ):
                            shutil.copyfileobj(source, output)
                        temporary.replace(target)
        except zipfile.BadZipFile:
            destination.unlink(missing_ok=True)
            raise RuntimeError("Corrupt ZIP removed; run again to resume.") from None
        if not complete(folder, item):
            raise RuntimeError("Incomplete population raster: " + item["filename"])
        destination.unlink(missing_ok=True)
        print("Ready: " + item["filename"][:3], flush=True)
    if not all(complete(folder, item) for item in selected):
        raise RuntimeError("Population raster completeness check failed.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("countries", nargs="*")
    parser.add_argument(
        "--directory", default=os.environ.get("GRUMP_DATA", "/data/grump")
    )
    args = parser.parse_args()
    try:
        ensure_data(args.directory, args.countries)
    except (RuntimeError, requests.RequestException) as error:
        parser.exit(1, str(error) + "\n")
