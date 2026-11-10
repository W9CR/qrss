"""PyInstaller entry point for the QBS bundle.

The app lives in the qrss package, which cannot be run directly from a script
(it uses relative imports), so the frozen build starts here instead.
Arguments are the same as `python3 -m qrss`: a codeplug path and/or --sim.
"""
from qrss.app import main

main()
