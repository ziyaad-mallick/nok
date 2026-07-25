"""`python -m nok` entry point -> nok.app.main()."""
import sys

from .app import main

if __name__ == "__main__":
    sys.exit(main())
