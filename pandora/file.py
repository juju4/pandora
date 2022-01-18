import hashlib
import os
import re
import shutil
import traceback

from datetime import datetime
from functools import cached_property
from io import BytesIO
from pathlib import Path
from typing import Optional, List, Union, Dict, cast, Set
from zipfile import ZipFile

import exif  # type: ignore
import exiftool  # type: ignore
import fitz  # type: ignore
import magic
import textract  # type: ignore

from eml_parser import EmlParser

from .default import get_config
from .helpers import make_bool, make_bool_for_redis
from .storage_client import Storage
from .text_parser import TextParser


class File:
    MIME_TYPE_EQUAL: Dict[str, List[str]] = {
        'application/zip': ['ARC', 'zip'],
        'application/java-archive': ['ARC', 'jar'],
        'application/x-7z-compressed': ['ARC', '7z'],
        'text/css': ['CSS', 'css'],
        'text/csv': ['CSV', 'csv'],
        'application/msword': ['DOC', 'doc'],
        'application/vnd.oasis.opendocument.text': ['DOC', 'odt'],
        'application/vnd.openxmlformats-officedocument.wordprocessingml.document': ['DOC', 'docx'],
        'message/rfc822': ['EML', 'eml'],
        'text/html': ['HTM', 'html'],
        'application/xhtml+xml': ['HTM', 'html'],
        'image/bmp': ['IMG', 'bmp'],
        'image/gif': ['IMG', 'gif'],
        'image/x-icon': ['IMG', 'ico'],
        'image/jpeg': ['IMG', 'jpg'],
        'image/png': ['IMG', 'png'],
        'image/svg+xml': ['IMG', 'svg'],
        'image/tiff': ['IMG', 'tiff'],
        'image/webp': ['IMG', 'webp'],
        'application/vnd.ms-outlook': ['MSG', 'msg'],
        'application/pdf': ['PDF', 'pdf'],
        'application/vnd.oasis.opendocument.presentation': ['PPT', 'ppt'],
        'application/vnd.ms-powerpoint': ['PPT', 'ppt'],
        'application/vnd.openxmlformats-officedocument.presentationml.presentation': ['PPT', 'pptx'],
        'application/mspowerpoint': ['PPT', 'ppt'],
        'application/powerpoint': ['PPT', 'ppt'],
        'application/x-mspowerpoint': ['PPT', 'ppt'],
        'text/rtf': ['RTF', 'rtf'],
        'application/x-javascript': ['JSC', 'js'],
        'application/javascript': ['JSC', 'js'],
        'text/javascript': ['JSC', 'js'],
        'text/plain': ['TXT', 'txt'],
        'application/vnd.ms-excel': ['XLS', 'xls'],
        'application/vnd.oasis.opendocument.spreadsheet': ['XLS', 'xls'],
        'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet': ['XLS', 'xlsx']
    }

    TYPE_EXTENSIONS: Dict[str, Set[str]] = {
        'ARC': {'.zip', '.tar', '.gz', '.bz2', '.bz', '.rar', '.7z'},
        'BIN': {'.bin', '.iso'},
        'CSS': {'.css'},
        'CSV': {'.csv'},
        'DOC': {'.doc', '.docx', '.odt'},
        'EML': {'.eml'},
        'EXE': {'.exe', '.dll'},
        'HTM': {'.html', '.html', '.xht', '.xhtml'},
        'IMG': {'.png', '.gif', '.bmp', '.jpg', '.jpeg', '.ico'},
        'JSC': {'.js'},
        'MSG': {'.msg'},
        'PDF': {'.pdf'},
        'PPT': {'.ppt', '.pptx'},
        'RTF': {'.rtf'},
        'SCR': {'.vb', '.vbs', '.php', '.ps1'},
        'TXT': {'.txt'},
        'XLS': {'.xls', '.xlsx', '.ods'}
    }
    TYPE_ICONS: Dict[str, str] = {
        'ARC': 'file-zip',
        'BIN': 'binary',
        'CSS': 'file-css',
        'CSV': 'file-excel',
        'DOC': 'file-word',
        'EML': 'email',
        'EXE': 'file-exe',
        'HTM': 'file-html5',
        'IMG': 'file-jpg',
        'JSC': 'file-javascript',
        'MSG': 'email',
        'PDF': 'file-pdf',
        'PPT': 'file-powerpoint',
        'RTF': 'file-document',
        'SCR': 'file-code',
        'TXT': 'file-text',
        'XLS': 'file-excel',
    }
    TYPE_INFO: Dict[str, str] = {
        'ARC': 'Archive file',
        'BIN': 'Binary file',
        'CSS': 'Cascading Style Sheet',
        'CSV': 'MS Excel document',
        'DOC': 'MS Word document',
        'EML': 'Message file',
        'EXE': 'Executable file',
        'HTM': 'HTML file',
        'IMG': 'Image file',
        'JSC': 'JavaScript file',
        'MSG': 'Microsoft Outlook message',
        'PDF': 'PDF file',
        'PPT': 'MS PowerPoint document',
        'RTF': 'Rich Text Format document',
        'SCR': 'Script file',
        'TXT': 'Text file',
        'XLS': 'MS Excel document',
    }
    OLETOOLS_TYPES: Set[str] = {'DOC', 'PPT', 'RTF', 'XLS'}
    UNOCONV_TYPES: Set[str] = {'CSS', 'DOC', 'HTM', 'JSC', 'PPT', 'RTF', 'TXT', 'XLS'}
    FOLDER_MODE = 0o2775
    FILE_MODE = 0o0664
    SUBPROCESS_TIMEOUT: int = 30

    DATA_CHARSETS: List[str] = [
        'utf8',
        'latin1',
        'ascii'
    ]

    def __init__(self, path: Union[Path, str], uuid: str, original_filename: str, *,
                 save_date: Optional[Union[str, datetime]]=None,
                 md5: Optional[str]=None, sha1: Optional[str]=None, sha256: Optional[str]=None,
                 size: Optional[Union[int, str]]=None,
                 deleted: Union[bool, int, str]=False):
        """
        Generate File object.
        :param path: absolute file path
        :param uuid: uuid based on file MD5
        :param original_filename: original filename as uploaded
        :param save_date: file save date
        :param md5: MD5 signature of file content
        :param sha1: SHA1 signature of file content
        :param sha256: SHA256 signature of file content
        :param size: file size in bytes
        :param deleted: whether if the file has been deleted
        """

        self.storage = Storage()

        if isinstance(path, str):
            self.path: Path = Path(path)
        else:
            self.path = path

        self.uuid: str = uuid
        self.original_filename: str = original_filename
        self.deleted: bool = make_bool(deleted)

        if not self.path.exists():
            self.deleted = True

        self._md5: Optional[str] = None
        self._sha1: Optional[str] = None
        self._sha256: Optional[str] = None
        self._text: Optional[str] = None
        self._size: int = 0
        if self.deleted:
            # Hashes should have been stored and must be present in the parameter
            # If the file is still on disk, they're initialized ondemand
            if not md5 or not sha1 or not sha256:
                raise Exception(f'The hashes should have been initialized. md5: {md5}, sha1: {sha1}, sha256: {sha256}')
            if not size:
                raise Exception(f'The size {size} should have been initialized.')

            self.md5: str = md5
            self.sha1: str = sha1
            self.sha256: str = sha256
            self.size: int = int(size)

        if save_date:
            if isinstance(save_date, str):
                self.save_date = datetime.fromisoformat(save_date)
            else:
                self.save_date = save_date
        else:
            self.save_date = datetime.now()

    def store(self) -> None:
        self.storage.set_file(self.to_dict)

    def make_previews(self) -> None:
        # NOTE: For images uploaded by user, re-create them so the images downloaded from the web are safe(r)
        if self.is_pdf:
            doc = fitz.open(self.path)
            digits = len(str(doc.page_count))
            for page in doc:
                pix = page.get_pixmap()
                img_name = self.directory / f"preview-{page.number:0{digits}}.png"
                pix.save(img_name)

    @property
    def previews(self) -> List[Path]:
        return sorted(self.directory.glob('preview-*.png'))

    @property
    def previews_archive(self) -> Optional[Path]:
        if not self.previews:
            return None
        archive_file = self.directory / 'previews.zip'
        if not archive_file.exists():
            with ZipFile(archive_file, 'w') as zipObj:
                for preview in self.previews:
                    zipObj.write(preview, arcname=preview.name)

        return archive_file

    @property
    def directory(self) -> Path:
        return self.path.parent

    @cached_property
    def data(self) -> BytesIO:
        """
        Property to get file content in binary format.
        :return (bytes|None): file content or None if file is not reachable
        """
        with self.path.open('rb') as f:
            return BytesIO(f.read(get_config('generic', 'max_file_size')))

    @property
    def to_dict(self) -> Dict[str, Union[str, int]]:
        return {
            'path': str(self.path),
            'uuid': self.uuid,
            'md5': self.md5,
            'sha1': self.sha1,
            'sha256': self.sha256,
            'size': self.size,
            'original_filename': self.original_filename,
            'save_date': self.save_date.isoformat(),
            'deleted': make_bool_for_redis(self.deleted)
        }

    @property
    def to_web(self) -> Dict[str, Union[str, int, List[str]]]:
        to_return = cast(Dict[str, Union[str, int, List[str]]], self.to_dict)
        to_return['previews'] = [str(path) for path in self.previews]
        return to_return

    def __str__(self) -> str:
        return str(self.path)

    @property
    def md5(self) -> str:
        """
        Property to get hexadecimal form of file content MD5 signature.
        :return (str|None): hexadecimal string or None if file is not reachable
        """
        if self._md5 is None:
            self._md5 = hashlib.md5(self.data.getvalue()).hexdigest() if self.data is not None else None
        return self._md5

    @md5.setter
    def md5(self, value: str):
        self._md5 = value

    @property
    def sha1(self) -> str:
        """
        Property to get hexadecimal form of file content SHA1 signature.
        :return (str): hexadecimal string or None if file is not reachable
        """
        if self._sha1 is None:
            self._sha1 = hashlib.sha1(self.data.getvalue()).hexdigest() if self.data is not None else None
        return self._sha1

    @sha1.setter
    def sha1(self, value: str):
        self._sha1 = value

    @property
    def sha256(self) -> str:
        """
        Property to get hexadecimal form of file content SHA256 signature.
        :return (str): hexadecimal string or None if file is not reachable
        """
        if self._sha256 is None:
            self._sha256 = hashlib.sha256(self.data.getvalue()).hexdigest() if self.data is not None else None
        return self._sha256

    @sha256.setter
    def sha256(self, value: str):
        self._sha256 = value

    @cached_property
    def mime_type(self) -> str:
        return magic.from_buffer(self.data.getvalue(), mime=True)

    def delete(self) -> None:
        """
        Delete from disk uploaded file and all other files in the same directory
        """
        if self.directory and self.directory.exists():
            shutil.rmtree(self.directory, ignore_errors=True)
        self.deleted = True

    @property
    def size(self) -> int:
        """
        Return size of file content
        :return: file content size
        """
        if not self._size and self.data:
            self._size = self.data.getbuffer().nbytes
        return self._size

    @size.setter
    def size(self, value: int):
        self._size = value

    @cached_property
    def type(self) -> str:
        """
        Guess file type from mimeType or extension.
        :return (str): file type or None if file is not reachable
        """
        # NOTE: maybe store it in the db, same as size
        # EML type file by extension to avoid magic library detection trouble
        extension = os.path.splitext(self.path)[1]
        if extension == ".eml":
            return "EML"

        # Guess type from mime-type
        if self.mime_type in self.MIME_TYPE_EQUAL:
            return self.MIME_TYPE_EQUAL[self.mime_type][0]

        # Guess type from extension
        for type_, extensions in self.TYPE_EXTENSIONS.items():
            if self.path.suffix in extensions:
                return type_

        # Default type to BIN (??)
        return 'BIN'

    @cached_property
    def _extension_for_textract(self) -> Optional[str]:
        """
        Textract expects a specific list of extensions, sanitize the one we have.
        :return (str): file type or None if file is not reachable
        """
        # Guess extension from mime-type
        for mime_type in self.MIME_TYPE_EQUAL:
            if self.mime_type == mime_type:
                return self.MIME_TYPE_EQUAL[mime_type][1]

        # Guess type from extension
        if self.path.suffix:
            return self.path.suffix

        # Default extension to None
        return None

    @cached_property
    def text(self) -> str:
        """
        Property to get file text content.
        :return (str): text content
        """
        try:
            if self.type == 'HTM':
                return self.data.getvalue().decode(errors='replace')
            else:
                # Use of textract module for all file types
                return textract.process(self.path, extension=self._extension_for_textract).decode(errors='replace')

        except textract.exceptions.ShellError:
            if self.is_doc:
                # Specific error when doc file is too small for some obscure reason
                # TODO try something with catdoc
                pass
        except textract.exceptions.ExtensionNotSupported:
            # Extension not supported by textract
            pass
        except BaseException as e:
            self.error = 'Text conversion error'
            self.error_trace = f'{e}\n{traceback.format_exc()}'
        return ''

    def convert(self, force: bool=False):
        """
        Convert file in image and save it in tasks folder.
        :param (bool) force: if True do convert even if it is already done
        """
        pass
        """
        if not force and self.converted:
            return
        self.converted = True
        """
        # TODO
        # * UNOCONV_TYPES -> PDF
        # * copy pdf to new name ??
        # * email MSG format to EML, store EML as is if already in this format, just decode
        # * email content: convert to pdf and then to images, especially if HTML. Uses imgkit (?)
        # * if image, store as image for preview => make a new file instead for safety reason
        # * pdf to png (done in make preview)
        # * pdf -> png -> pdf to have a safe thing to dl
        # * text to png
        # 8 compress all images to zip (done elsewhere)

    @property
    def links(self) -> Set[str]:
        """
        Extract links from file content
        :return (set): set of links (ips, urls, ibans, emails, hostnames)
        """
        links = set()
        parsed = ""

        # Try to extract eml|msg observables
        try:
            if self.eml_data:
                for value in self.eml_data['body'][0]['content']:
                    parsed += value
                parsed += ' '
                for val in self.eml_data['header']['from']:
                    parsed += val
                parsed += ' '
                for va in self.eml_data['header']['to']:
                    parsed += va

            tp = TextParser(parsed.replace('\r\n', ''))
            links.update(tp.ips)
            links.update(tp.ibans)
            links.update(tp.urls)
            links.update(tp.hostnames)
            links.update(tp.emails)
        except BaseException:
            pass

        # Try to extract links from text
        if self.text:
            tp = TextParser(self.text.replace('\r\n', ''))
            links.update(tp.ips)
            links.update(tp.ibans)
            links.update(tp.urls)
            links.update(tp.hostnames)
            links.update(tp.emails)

        # TODO: extract stuff from pdfs, was using PyPDF4, which is dead.
        return links

    @cached_property
    def eml_data(self) -> Optional[Dict]:
        if not self.is_eml:
            return None
        ep = EmlParser(include_raw_body=True, include_attachment_data=True)
        return ep.decode_email(eml_file=self.path)

    @cached_property
    def metadata(self) -> Dict[str, str]:
        """
        Get file metadata.
        :return (dict): metadata
        """
        if self.is_image:
            exif_image = exif.image(self.data)
            if exif_image.has_exif:
                return exif_image.get_all()
            return {}
        else:
            metadata: Dict[str, str] = {}
            # FIXME: need binary - https://pypi.org/project/PyExifTool/
            with exiftool.ExifTool() as et:
                for key, value in et.get_metadata_batch([self.path])[0].items():
                    if any(key.lower().startswith(word) for word in ('sourcefile', 'exiftool:', 'file:')):
                        continue
                    key = key.split(':')[-1]
                    key = re.sub(r"([A-Z]+)([A-Z][a-z])", r'\1 \2', key)
                    key = re.sub(r"([a-z\d])([A-Z])", r'\1 \2', key)
                    self.metadata[key] = value
            return metadata

    @property
    def icon(self) -> Optional[str]:
        """
        Get web icon for file type.
        :return (str|None): icon name or None if unknown type
        """
        return self.TYPE_ICONS.get(self.type)

    @property
    def info(self) -> Optional[str]:
        """
        Get type info for web display.
        :return (str|None): type info or None if unknown type
        """
        return self.TYPE_INFO.get(self.type)

    @property
    def is_oletools_concerned(self) -> bool:
        """
        Whether this file is concerned by oletools scans.
        :return (bool): boolean
        """
        return self.type in self.OLETOOLS_TYPES

    @property
    def is_unoconv_concerned(self) -> bool:
        """
        Whether this file is concerned by unoconv.
        :return (bool): boolean
        """
        return self.type in self.UNOCONV_TYPES

    @property
    def is_archive(self) -> bool:
        """
        Whether this file is an archive.
        :return (bool): boolean
        """
        return self.type == 'ARC'

    @property
    def is_rtf(self) -> bool:
        """
        Whether this file is a RTF.
        :return (bool): boolean
        """
        return self.type == 'RTF'

    @property
    def is_pdf(self) -> bool:
        """
        Whether this file is a PDF.
        :return (bool): boolean
        """
        return self.type == 'PDF'

    @property
    def is_eml(self) -> bool:
        """
        Whether this file is an EML.
        :return (bool): boolean
        """
        return self.type == 'EML'

    @property
    def is_msg(self) -> bool:
        """
        Whether this file is a MSG.
        :return (bool): boolean
        """
        return self.type == 'MSG'

    @property
    def is_txt(self) -> bool:
        """
        Whether this file is a TXT.
        :return (bool): boolean
        """
        return self.type == 'TXT'

    @property
    def is_doc(self) -> bool:
        """
        Whether this file is a DOC.
        :return (bool): boolean
        """
        return self.type == 'DOC'

    @property
    def is_image(self) -> bool:
        """
        Whether this file is an image.
        :return (bool): boolean
        """
        return self.type == 'IMG'

    @property
    def is_html(self) -> bool:
        """
        Whether this file is an HTML.
        :return (bool): boolean
        """
        return self.type == 'HTM'

    @property
    def is_script(self) -> bool:
        """
        Whether this file is a script.
        :return (bool): boolean
        """
        return self.type == 'SCR'

    @property
    def is_javascript(self) -> bool:
        """
        Whether this file is a javascript.
        :return (bool): boolean
        """
        return self.type == 'JSC'

    @property
    def is_executable(self) -> bool:
        """
        Whether this file is an exe.
        :return (bool): boolean
        """
        return self.type == 'EXE'