"""Allow `python -m bridle ...` as an alternative to the `bridle` console script."""
from bridle.cli import app

if __name__ == "__main__":
    app()
