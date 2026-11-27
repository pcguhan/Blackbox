"""Participant access policy and allowlist import."""

import csv
import io
import re
import zipfile

from openpyxl import load_workbook

from models import db
from models.access_allowlist import AccessAllowlistEntry
from models.settings import Settings


SETTING = 'exclusive_access_enabled'
BLOCKED_MESSAGE = "Event going on right now, you don't have access."
DEACTIVATED_MESSAGE = 'Your account has been deactivated'
MAX_UPLOAD_BYTES = 5 * 1024 ** 2
MAX_UNCOMPRESSED_BYTES = 50 * 1024 ** 2
MAX_ROWS = 20_000
MAX_CELLS = 500_000
MAX_EMAILS = 20_000

EMAIL_TOKEN = re.compile(r"[\w.!#$%&'*+/=?^`{|}~@-]+")
LOCAL_ATOM = r"[\w!#$%&'*+/=?^`{|}~-]+"
DOMAIN_LABEL = r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?'
EMAIL = re.compile(rf'{LOCAL_ATOM}(?:\.{LOCAL_ATOM})*@(?:{DOMAIN_LABEL}\.)+[a-z]{{2,63}}', re.ASCII)
DOCTYPE_MARKERS = tuple(
    '<!DOCTYPE'.encode(encoding)
    for encoding in ('ascii', 'utf-16-le', 'utf-16-be', 'utf-32-le', 'utf-32-be', 'cp037')
)


class AllowlistError(ValueError):
    pass


def is_enabled():
    # Access decisions need the committed setting even if cache invalidation fails.
    value = db.session.scalar(db.select(Settings.value).where(Settings.key == SETTING))
    return Settings._convert_value(value, 'bool', False)


def set_enabled(enabled):
    Settings.set(SETTING, enabled, 'bool', 'Restrict participant access to listed email addresses')


def is_email_allowed(email):
    """Return whether an email can participate without an administrator exemption."""
    if not is_enabled():
        return True
    if not email:
        return False
    return db.session.query(
        AccessAllowlistEntry.query.filter_by(email=email.strip().lower()).exists()
    ).scalar()


def enforce_for_user(user):
    """Return whether an existing account may sign in or keep using its session."""
    return bool(user.is_active and (user.is_admin or is_email_allowed(user.email)))


def _xlsx_rows(data):
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            parts = archive.infolist()
            if sum(part.file_size for part in parts) > MAX_UNCOMPRESSED_BYTES:
                raise AllowlistError('The workbook is too large once uncompressed.')
            # Openpyxl follows workbook relationships, so check every part for
            # DTDs even when its name has an unconventional extension.
            for part in parts:
                content = archive.read(part)
                if any(marker in content for marker in DOCTYPE_MARKERS):
                    raise AllowlistError('Workbook XML cannot contain document type declarations.')
        workbook = load_workbook(io.BytesIO(data), read_only=True, data_only=True)
        try:
            for sheet in workbook.worksheets:
                sheet.reset_dimensions()
                yield from sheet.iter_rows(values_only=True)
        finally:
            workbook.close()
    except AllowlistError:
        raise
    except Exception as error:
        # Malformed ZIP, XML and spreadsheet content raise different exceptions.
        raise AllowlistError('The .xlsx workbook could not be read.') from error


def _csv_rows(data):
    try:
        content = data.decode('utf-8-sig')
    except UnicodeDecodeError:
        content = data.decode('latin-1')
    try:
        yield from csv.reader(io.StringIO(content, newline=''), strict=True)
    except csv.Error as error:
        raise AllowlistError('The CSV file could not be read.') from error


def parse_allowlist(filename, data):
    """Return sorted unique emails and the count of invalid address candidates."""
    if len(data) > MAX_UPLOAD_BYTES:
        raise AllowlistError('The file must be 5 MB or smaller.')
    name = (filename or '').lower()
    if name.endswith('.xlsx'):
        rows = _xlsx_rows(data)
    elif name.endswith('.csv'):
        rows = _csv_rows(data)
    else:
        raise AllowlistError('Upload an Excel (.xlsx) or CSV file.')

    emails = set()
    invalid_count = cell_count = 0
    try:
        for row_number, row in enumerate(rows, start=1):
            if row_number > MAX_ROWS:
                raise AllowlistError(f'The file has more than {MAX_ROWS:,} rows.')
            cell_count += len(row)
            if cell_count > MAX_CELLS:
                raise AllowlistError(f'The file has more than {MAX_CELLS:,} cells.')
            for cell in row:
                if not isinstance(cell, str) or '@' not in cell:
                    continue
                for token in EMAIL_TOKEN.findall(cell):
                    if '@' not in token:
                        continue
                    email = token.lower()
                    if len(email) <= 120 and EMAIL.fullmatch(email) and email.index('@') <= 64:
                        emails.add(email)
                    else:
                        invalid_count += 1
                if len(emails) > MAX_EMAILS:
                    raise AllowlistError(f'The file lists more than {MAX_EMAILS:,} addresses.')
    finally:
        rows.close()
    return sorted(emails), invalid_count


def replace_allowlist(emails, keep_existing=False):
    if keep_existing:
        existing = set(db.session.scalars(db.select(AccessAllowlistEntry.email)))
    else:
        db.session.execute(db.delete(AccessAllowlistEntry))
        existing = set()
    additions = set(emails) - existing
    db.session.add_all(AccessAllowlistEntry(email=email) for email in sorted(additions))
    db.session.commit()
    return len(existing) + len(additions)


def clear_allowlist():
    db.session.execute(db.delete(AccessAllowlistEntry))
    db.session.commit()
