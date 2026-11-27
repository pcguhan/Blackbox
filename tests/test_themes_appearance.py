import re
import unittest

from tests.ui_fixture import Settings, seed


class ThemesAppearanceTests(unittest.TestCase):
    def setUp(self):
        self.app = seed()
        self.visitor = self.app.test_client()

    def csrf_token(self, client, path):
        page = client.get(path)
        self.assertEqual(page.status_code, 200)
        return re.search(r'name="csrf-token" content="([^"]+)"', page.text).group(1)

    def login(self, role):
        client = self.app.test_client()
        response = client.post('/login', data={
            'username': f'preview-{role}',
            'password': 'PreviewOnly123!',
            'csrf_token': self.csrf_token(client, '/login'),
        })
        self.assertEqual(response.status_code, 302)
        return client

    def assert_theme(self, client, path, palette, font='default'):
        page = client.get(path)
        self.assertEqual(page.status_code, 200)
        mode = 'light' if palette == 'paper' else 'dark'
        self.assertIn(f'data-bs-theme="{mode}" data-theme="{palette}" data-font="{font}"', page.text)
        self.assertIn('class="technology-background"', page.text)
        self.assertIn('/static/images/background.png', page.text)
        return page.text

    def test_each_palette_renders_on_participant_and_admin_pages(self):
        player = self.login('player')
        admin = self.login('admin')
        fork_palettes = ('default', 'graphite', 'forest', 'copper', 'paper')
        added_palettes = ('navy', 'neon')
        design = self.visitor.get('/static/css/design.css', buffered=True).text
        extra = self.visitor.get('/static/css/themes-extra.css', buffered=True).text
        self.assertIn('.technology-background::before', extra)
        self.assertIn('background-color: rgba(var(--panel-rgb)', extra)
        with self.visitor.get('/static/images/background.png') as background:
            self.assertEqual(background.status_code, 200)

        for palette in fork_palettes + added_palettes:
            with self.subTest(palette=palette):
                with self.app.app_context():
                    Settings.set('site_theme', palette)
                self.assert_theme(player, '/teams/1', palette)
                admin_page = self.assert_theme(admin, '/admin/settings', palette)
                self.assertRegex(admin_page, rf'<option value="{palette}"\s+selected>')
                self.assertIn(f'[data-theme="{palette}"]', design if palette in fork_palettes else extra)
                with self.app.app_context():
                    self.assertEqual(Settings.get('site_theme'), palette)

    def test_defaults_and_legacy_alias_leave_saved_data_intact(self):
        self.assert_theme(self.visitor, '/', 'default')
        for saved, rendered in [('forest', 'forest'), ('light', 'paper'), ('unknown', 'default')]:
            with self.subTest(saved=saved):
                with self.app.app_context():
                    Settings.set('site_theme', saved)
                    Settings.set('site_font', 'unsupported')
                    Settings.set('custom_background_enabled', True, 'bool')
                    Settings.set('custom_background_css', 'body { background-color: var(--canvas); }')
                page = self.assert_theme(self.visitor, '/', rendered)
                self.assertIn('id="custom-background-theme"', page)
                with self.app.app_context():
                    self.assertEqual(Settings.get('site_theme'), saved)
                    self.assertEqual(Settings.get('site_font'), 'unsupported')

    def test_admin_saves_both_new_palettes_and_fonts(self):
        admin = self.login('admin')
        token = self.csrf_token(admin, '/admin/settings')
        for endpoint, palette, font in (
            ('/admin/settings/appearance', 'navy', 'georgia'),
            ('/admin/settings/background-theme', 'neon', 'verdana'),
        ):
            with self.subTest(palette=palette):
                response = admin.post(endpoint, data={
                    'csrf_token': token,
                    'site_theme': palette,
                    'site_font': font,
                }, follow_redirects=True)
                self.assertEqual(response.status_code, 200)
                self.assertIn('Appearance settings saved.', response.text)
                with self.app.app_context():
                    self.assertEqual(Settings.get('site_theme'), palette)
                    self.assertEqual(Settings.get('site_font'), font)
                self.assert_theme(self.visitor, '/', palette, font)
                self.assert_theme(admin, '/admin/settings', palette, font)

    def test_invalid_save_keeps_all_existing_appearance_settings(self):
        admin = self.login('admin')
        token = self.csrf_token(admin, '/admin/settings')
        with self.app.app_context():
            Settings.set('site_theme', 'copper')
            Settings.set('site_font', 'courier')
            Settings.set('custom_background_enabled', True, 'bool')
            Settings.set('custom_background_css', 'body { background-color: var(--canvas); }')

        for palette, font in [('unknown', 'georgia'), ('neon', 'unsupported')]:
            with self.subTest(palette=palette, font=font):
                response = admin.post('/admin/settings/appearance', data={
                    'csrf_token': token,
                    'site_theme': palette,
                    'site_font': font,
                    'custom_background_css': '',
                }, follow_redirects=True)
                self.assertEqual(response.status_code, 200)
                self.assertIn('Choose one of the available', response.text)
                self.assert_theme(self.visitor, '/', 'copper', 'courier')
                with self.app.app_context():
                    self.assertTrue(Settings.get('custom_background_enabled'))
                    self.assertEqual(Settings.get('custom_background_css'), 'body { background-color: var(--canvas); }')

    def test_appearance_is_admin_only(self):
        player = self.login('player')
        payload = {'site_theme': 'neon', 'site_font': 'georgia'}
        self.assertEqual(self.visitor.post('/admin/settings/appearance', data=payload).status_code, 302)
        payload['csrf_token'] = self.csrf_token(player, '/profile')
        self.assertEqual(player.post('/admin/settings/appearance', data=payload).status_code, 302)
        page = self.assert_theme(player, '/profile', 'default')
        self.assertNotIn('adminThemeSelect', page)
        self.assertNotIn('adminFontSelect', page)

    def test_cyscom_header_and_team_links_for_each_role(self):
        player = self.login('player')
        admin = self.login('admin')
        for client, path in [(self.visitor, '/'), (player, '/teams/1'), (admin, '/admin/settings')]:
            with self.subTest(path=path):
                page = self.assert_theme(client, path, 'default')
                self.assertIn('aria-label="Main navigation"', page)
                self.assertIn('alt="CYSCOM"', page)
                self.assertIn('rel="icon" type="image/webp" href="/static/images/cyscom-logo.webp"', page)
                self.assertNotIn('cyberarena', page.lower())

        player_page = player.get('/teams/1').text
        self.assertIn('class="nav-link" href="/teams/"', player_page)
        self.assertIn('aria-controls="userMenu"', player_page)
        self.assertIn('aria-controls="notificationsMenu"', player_page)
        self.assertIn('tabindex="0" role="region" aria-label="Recent notifications"', player_page)
        menu = player_page.split('id="userMenu"', 1)[1].split('</ul>', 1)[0]
        for href in ('/profile', '/guide', '/teams/1', '/logout'):
            self.assertIn(f'href="{href}"', menu)
        self.assertNotIn('href="/admin/"', menu)
        admin_menu = admin.get('/admin/settings').text.split('id="userMenu"', 1)[1].split('</ul>', 1)[0]
        self.assertIn('href="/admin/"', admin_menu)
        self.assertIn('href="/teams/"', admin_menu)
        self.assertEqual(player.get('/scoreboard/api/data').json[0]['name'], 'Packet Inspectors')
