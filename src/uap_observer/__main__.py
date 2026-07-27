"""Allow ``python -m uap_observer`` to invoke the CLI."""

from uap_observer.cli import main


if __name__ == "__main__":
    raise SystemExit(main())
