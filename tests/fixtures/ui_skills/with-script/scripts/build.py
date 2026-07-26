from pathlib import Path


Path(__file__).resolve().parents[1].joinpath("executed").write_text("ran")
