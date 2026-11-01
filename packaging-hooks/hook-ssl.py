"""Include companion trust data when building with a runtime that requires it."""
from pathlib import Path
import ssl

# Some Python distributions load this next to ssl.py during SSLContext creation.
# Retain that runtime's trust policy in the frozen application as well.
companions = Path(ssl.__file__).parent.glob('*-rejected-cas.pem')
datas = [(str(companion), '.') for companion in companions if companion.is_file()]
