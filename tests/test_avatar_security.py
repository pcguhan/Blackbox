from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from PIL import Image

from models.settings import Settings
from tests.ui_fixture import app, seed


AVATAR_API = '/api/profile/settings'
PAYLOAD = b'<html><script>example()</script></html>'
TOKEN = {'X-CSRF-Token': 'avatar-test-token'}


def picture(fmt='PNG', size=(64, 48), mode='RGB', **save):
    output = BytesIO()
    Image.new(mode, size, 'red' if mode == 'RGB' else 1).save(output, fmt, **save)
    return output.getvalue()


class AvatarSecurityTests(unittest.TestCase):
    def setUp(self):
        seed()
        root = Path(self.enterContext(TemporaryDirectory(dir=Path.cwd())))
        self.folder = root / 'avatars'
        self.logos = root / 'logos'
        self.enterContext(patch.dict(app.config, {
            'AVATAR_UPLOAD_FOLDER': self.folder, 'LOGO_UPLOAD_FOLDER': self.logos,
        }))
        self.client = app.test_client()
        self.login(2)

    def login(self, user):
        with self.client.session_transaction() as session:
            session.update(_user_id=str(user), _fresh=True, _csrf_token=TOKEN['X-CSRF-Token'])

    def post_image(self, url, field, data, name):
        response = self.client.post(url, data={field: (BytesIO(data), name)}, headers=TOKEN)
        response.request.input_stream.close()
        return response

    def upload(self, data, name):
        return self.post_image(AVATAR_API, 'avatar', data, name)

    def test_supported_formats_strip_trailing_content_and_send_safe_headers(self):
        for fmt, suffix, content_type in (
            ('PNG', 'png', 'image/png'), ('JPEG', 'jpeg', 'image/jpeg'), ('WEBP', 'webp', 'image/webp'),
        ):
            with self.subTest(fmt=fmt):
                response = self.upload(picture(fmt) + PAYLOAD, f'portrait.{suffix}')
                self.assertEqual(response.status_code, 200, response.json)
                (file,) = self.folder.iterdir()
                self.assertNotIn(PAYLOAD, file.read_bytes())
                with Image.open(file) as clean:
                    self.assertEqual(clean.format, fmt)
                url = response.json['avatar_url']
                with self.client.get(url) as avatar:
                    self.assertEqual(avatar.mimetype, content_type)
                    self.assertEqual(avatar.headers['X-Content-Type-Options'], 'nosniff')
                    self.assertIn("default-src 'none'", avatar.headers['Content-Security-Policy'])
                    self.assertIn('sandbox', avatar.headers['Content-Security-Policy'])
                self.assertEqual(app.test_client().get(url).status_code, 302)

    def test_dimensions_and_metadata_are_sanitized(self):
        exif = Image.Exif()
        exif[0x010F] = 'EXAMPLE-CAMERA-METADATA'
        response = self.upload(picture('JPEG', (2000, 1000), exif=exif), 'wide.jpg')
        self.assertEqual(response.status_code, 200)
        (file,) = self.folder.iterdir()
        with Image.open(file) as clean:
            self.assertEqual(clean.size, (512, 256))
            self.assertFalse(clean.getexif())

    def test_rejects_invalid_format_animation_and_oversize(self):
        animated = picture('PNG', save_all=True, append_images=[Image.new('RGB', (64, 48), 'blue')])
        cases = (
            (b'invalid', 'broken.png'), (picture(), 'fake.jpg'), (picture('GIF'), 'fake.png'),
            (picture(), 'picture.png.html'), (picture('PNG', (5000, 5000), mode='1'), 'big.png'),
            (animated, 'animated.png'),
        )
        for data, name in cases:
            with self.subTest(name=name):
                self.assertEqual(self.upload(data, name).status_code, 400)
        self.assertEqual(self.upload(b'x' * (app.config['AVATAR_MAX_BYTES'] + 1), 'big.png').status_code, 413)
        self.assertFalse(self.folder.exists())
        self.assertEqual(self.client.get('/avatars/invalid.html').status_code, 404)

    def test_public_event_logo_is_reencoded_and_invalid_upload_keeps_previous_logo(self):
        self.login(1)
        self.assertIn(b'/static/images/cyscom-logo.webp', self.client.get('/').data)
        url = '/admin/settings/event-config'
        response = self.post_image(url, 'ctf_logo', picture(size=(1500, 750)) + PAYLOAD, 'logo.png')
        self.assertEqual(response.status_code, 302)
        (file,) = self.logos.iterdir()
        with app.test_client().get('/uploads/' + file.name) as logo:
            self.assertEqual(logo.mimetype, 'image/png')
            self.assertNotIn(PAYLOAD, logo.data)
            with Image.open(BytesIO(logo.data)) as image:
                self.assertEqual(image.size, (1024, 512))
        self.post_image(url, 'ctf_logo', b'wrong contents', 'rejected.png')
        with app.app_context():
            self.assertEqual(Settings.get('ctf_logo'), file.name)
