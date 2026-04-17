/* ========================================
   Settings Tab - full pager config editor
   Sectioned by category; mirrors the pager settings panel.
   ======================================== */
'use strict';

var SettingsTab = {
    init() {
        var panel = document.getElementById('tab-settings');
        panel.innerHTML =
            '<div class="card card-accent">' +
              '<h3>System Actions</h3>' +
              '<div class="btn-row">' +
                '<button id="sys-restart" class="btn btn-gold">Restart UI</button>' +
                '<button id="sys-reboot" class="btn btn-muted">Reboot Pager</button>' +
                '<button id="sys-shutdown" class="btn btn-danger">Shutdown</button>' +
              '</div>' +
            '</div>' +
            '<div id="settings-form"></div>';
        this.bindSystem();
    },

    bindSystem() {
        document.getElementById('sys-restart').onclick = async () => {
            if (!await App.confirm('Restart the pagerctl_home UI?')) return;
            try { await App.post('/api/system/restart_ui'); App.toast('Restarting UI', 'success'); }
            catch (e) { App.toast('Failed: ' + e, 'error'); }
        };
        document.getElementById('sys-reboot').onclick = async () => {
            if (!await App.confirm('Reboot the whole pager?')) return;
            try { await App.post('/api/system/reboot'); App.toast('Rebooting', 'success'); }
            catch (e) { App.toast('Failed: ' + e, 'error'); }
        };
        document.getElementById('sys-shutdown').onclick = async () => {
            if (!await App.confirm('Power off the pager?')) return;
            try { await App.post('/api/system/shutdown'); App.toast('Shutting down', 'success'); }
            catch (e) { App.toast('Failed: ' + e, 'error'); }
        };
    },

    activate() {
        this.refresh();
    },

    deactivate() {},

    // Schema keys match the REAL settings.json on the pager
    // (verified against the live file — not invented).
    sections: [
        {
            title: 'Display',
            items: [
                {key: 'brightness',      label: 'Brightness %',       type: 'number', min: 1, max: 100},
                {key: 'dim_brightness',  label: 'Dim brightness %',   type: 'number', min: 0, max: 100},
                {key: 'dim_timeout',     label: 'Dim after (s, 0=off)',    type: 'number', min: 0},
                {key: 'screen_timeout',  label: 'Screen off after (s, 0=off)', type: 'number', min: 0},
            ]
        },
        {
            title: 'Sound & Haptics',
            items: [
                {key: 'sound_enabled',  label: 'Sound enabled',  type: 'bool'},
                {key: 'vibrate_enabled',label: 'Vibrate enabled',type: 'bool'},
                {key: 'geiger_sound',   label: 'Wardrive geiger click', type: 'bool'},
                {key: 'boot_sound',     label: 'Boot sound (rtttl name)', type: 'text'},
            ]
        },
        {
            title: 'GPS',
            items: [
                {key: 'gps_enabled', label: 'GPS enabled', type: 'bool'},
                {key: 'gps_device',  label: 'GPS device path', type: 'text'},
                {key: 'gps_baud',    label: 'GPS baud (auto or 4800/9600/...)', type: 'text'},
            ]
        },
        {
            title: 'Wardrive',
            items: [
                {key: 'wardrive_autostart', label: 'Auto-start on boot', type: 'bool'},
                {key: 'scan_interface',     label: 'Scan interface (STA)', type: 'text'},
                {key: 'capture_interface',  label: 'Monitor interface',    type: 'text'},
                {key: 'scan_mode',          label: 'Scan mode',            type: 'text'},
                {key: 'scan_interval',      label: 'Scan interval (s)',    type: 'number', min: 1},
                {key: 'hop_speed',          label: 'Channel hop speed (s)',type: 'number', min: 0.1},
                {key: 'scan_2_4ghz',        label: 'Scan 2.4 GHz',         type: 'bool'},
                {key: 'scan_5ghz',          label: 'Scan 5 GHz',           type: 'bool'},
                {key: 'scan_6ghz',          label: 'Scan 6 GHz',           type: 'bool'},
                {key: 'channel_hop',        label: 'Channel hopping',      type: 'bool'},
                {key: 'capture_enabled',    label: 'Capture enabled',      type: 'bool'},
                {key: 'pcap_enabled',       label: 'PCAP handshake dump',  type: 'bool'},
                {key: 'wigle_enabled',      label: 'Wigle export (.csv)',  type: 'bool'},
                {key: 'wigle_api_name',     label: 'Wigle API Name',       type: 'text'},
                {key: 'wigle_api_token',    label: 'Wigle API Token',      type: 'text'},
            ]
        },
        {
            title: 'Hotspot',
            items: [
                {key: 'hotspot_ssid',     label: 'Hotspot SSID',     type: 'text'},
                {key: 'hotspot_password', label: 'Hotspot password', type: 'text'},
            ]
        },
        {
            title: 'Captive Portal',
            items: [
                {key: 'captive_ssid',              label: 'SSID',                 type: 'text'},
                {key: 'captive_ap_open',           label: 'Open AP (no password)',type: 'bool'},
                {key: 'captive_password',          label: 'WPA2 password',        type: 'text'},
                {key: 'captive_portal_template',   label: 'Portal template',      type: 'text'},
                {key: 'captive_portal_enabled',    label: 'Captive Portal',       type: 'bool'},
                {key: 'captive_spoof_enabled',     label: 'Spoof (R/O)',          type: 'bool'},
                {key: 'captive_mitm_enabled',      label: 'MITM (R/W)',           type: 'bool'},
                {key: 'captive_mirror_banner',     label: 'POC banner',           type: 'bool'},
                {key: 'captive_internet_allowed',  label: 'Real internet for clients', type: 'bool'},
                {key: 'captive_dns_hijack',        label: 'DNS hijack',           type: 'bool'},
            ]
        },
        {
            title: 'Web UI',
            items: [
                {key: 'web_server',   label: 'Web UI enabled', type: 'bool'},
                {key: 'web_port',     label: 'Web UI port',    type: 'number', min: 1, max: 65535},
                {key: 'web_password', label: 'Web password (empty = open)', type: 'text'},
            ]
        },
    ],

    async refresh() {
        try {
            var data = await App.api('/api/settings');
            var cfg = data.config || {};
            this._cfg = cfg;
            var form = document.getElementById('settings-form');
            var self = this;
            form.innerHTML = this.sections.map(function(sec) {
                var rows = sec.items.map(function(item) {
                    var val = cfg[item.key];
                    var input = '';
                    if (item.type === 'bool') {
                        var checked = val ? ' checked' : '';
                        input = '<label class="switch"><input type="checkbox" data-key="' +
                            App.esc(item.key) + '"' + checked + '/><span></span></label>';
                    } else if (item.type === 'number') {
                        input = '<input type="number" data-key="' + App.esc(item.key) +
                            '" value="' + App.esc(val != null ? val : '') + '"' +
                            (item.min != null ? ' min="' + item.min + '"' : '') +
                            (item.max != null ? ' max="' + item.max + '"' : '') + '/>';
                    } else {
                        input = '<input type="text" data-key="' + App.esc(item.key) +
                            '" value="' + App.esc(val != null ? val : '') + '"/>';
                    }
                    return '<div class="form-row"><label>' + App.esc(item.label) + '</label>' + input + '</div>';
                }).join('');
                return '<div class="card card-accent">' +
                    '<h3>' + App.esc(sec.title) + '</h3>' + rows +
                    '<div class="btn-row" style="margin-top:10px">' +
                      '<button class="btn btn-gold" data-save-sec="' + App.esc(sec.title) + '">Save ' + App.esc(sec.title) + '</button>' +
                    '</div></div>';
            }).join('');

            form.querySelectorAll('[data-save-sec]').forEach(function(btn) {
                btn.onclick = () => self.saveSection(btn.getAttribute('data-save-sec'));
            });
        } catch (e) {
            App.toast('Failed to load settings: ' + e, 'error');
        }
    },

    async saveSection(title) {
        var sec = this.sections.find(function(s) { return s.title === title; });
        if (!sec) return;
        var body = {};
        var form = document.getElementById('settings-form');
        sec.items.forEach(function(item) {
            var input = form.querySelector('[data-key="' + item.key + '"]');
            if (!input) return;
            if (item.type === 'bool') {
                body[item.key] = input.checked;
            } else if (item.type === 'number') {
                var n = parseFloat(input.value);
                if (!isNaN(n)) body[item.key] = n;
            } else {
                body[item.key] = input.value;
            }
        });
        try {
            await App.post('/api/settings', body);
            App.toast(title + ' saved', 'success');
        } catch (e) {
            App.toast('Save failed: ' + e, 'error');
        }
    }
};

App.registerTab('settings', SettingsTab);
