"""Identity display and return URLs shared by sign-in providers."""
import re
from urllib.parse import unquote


_ROLL_NUMBER = re.compile(r'\b[0-9]{2}[A-Z]{3}[0-9]{4}\b', re.ASCII | re.IGNORECASE)


def split_vit_name(full_name, username):
    """Return the name and an optional uppercased VIT roll number."""
    name = full_name or ''
    match = _ROLL_NUMBER.search(name)
    if match:
        name = re.sub(r'\(\s*\)', '', _ROLL_NUMBER.sub(' ', name))
    name = ' '.join(name.split())
    if name.casefold() == (username or '').casefold() and re.search(r'[._+@\d]', name):
        name = ''
    return name.title() or 'Participant', match.group().upper() if match else None


def local_next(value):
    """Accept only absolute paths within this site, including after decoding."""
    if not value or not value.startswith('/'):
        return None
    decoded = unquote(value)
    if decoded.startswith('//') or '\\' in decoded or any(ord(char) < 32 or ord(char) == 127 for char in decoded):
        return None
    return value
