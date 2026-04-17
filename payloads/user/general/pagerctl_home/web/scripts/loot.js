/* ========================================
   Loot Tab - download/delete credentials, wigle, pcap, logs
   ======================================== */
'use strict';

var LootTab = {
    init() {
        var panel = document.getElementById('tab-loot');
        panel.innerHTML =
            '<div class="subnav">' +
              '<button data-sub="credentials" class="sub-btn active">Credentials</button>' +
              '<button data-sub="wigle" class="sub-btn">Wigle CSVs</button>' +
              '<button data-sub="pcap" class="sub-btn">PCAPs</button>' +
              '<button data-sub="logs" class="sub-btn">Logs</button>' +
            '</div>' +
            '<div class="card card-accent">' +
              '<div class="btn-row" style="margin-bottom:8px">' +
                '<button id="loot-refresh" class="btn btn-muted btn-sm">Refresh</button>' +
              '</div>' +
              '<div id="loot-list" class="file-list"></div>' +
            '</div>';
        this.bindEvents();
        this._sub = 'credentials';
    },

    bindEvents() {
        var self = this;
        document.querySelectorAll('.sub-btn').forEach(function(b) {
            b.onclick = function() {
                document.querySelectorAll('.sub-btn').forEach(function(x) { x.classList.remove('active'); });
                b.classList.add('active');
                self._sub = b.dataset.sub;
                self.render();
            };
        });
        document.getElementById('loot-refresh').onclick = () => self.refresh();
    },

    activate() {
        this.refresh();
        App.startPolling('loot', () => this.refresh(), 5000);
    },

    deactivate() {},

    async refresh() {
        try {
            this._data = await App.api('/api/loot/list');
            this.render();
        } catch (e) {}
    },

    render() {
        var el = document.getElementById('loot-list');
        var data = this._data || {};
        var list = data[this._sub] || [];
        var kind = this._sub;
        var self = this;

        // Wigle subtab gets an upload control at the top
        var uploadRow = '';
        if (kind === 'wigle') {
            uploadRow = '<div class="upload-row" style="margin-bottom:10px">' +
                '<input type="file" id="wigle-upload-file" accept=".csv" />' +
                '<input type="text" id="wigle-upload-name" placeholder="filename (optional)" />' +
                '<button class="btn btn-gold btn-sm" id="wigle-upload-btn">Upload to WiGLE</button>' +
              '</div>';
        }

        if (!list.length) {
            el.innerHTML = uploadRow + '<div class="muted">nothing here yet</div>';
            if (kind === 'wigle') this.bindWigleUpload();
            return;
        }

        el.innerHTML = uploadRow + list.map(function(f) {
            var nm = f.name;
            var dl = '/api/loot/download/' + encodeURIComponent(kind) + '/' + encodeURIComponent(nm);
            var delBtn = (kind === 'logs') ? ''
                : '<button class="btn btn-danger btn-sm" data-del="' + App.esc(nm) + '">Delete</button>';
            var sendBtn = (kind === 'wigle')
                ? '<button class="btn btn-gold btn-sm" data-send="' + App.esc(nm) + '">→ WiGLE</button>'
                : '';
            var sizeStr = f.virtual ? 'virtual' : App.fmtBytes(f.size);
            return '<div class="file-row">' +
                '<span class="file-name">' + App.esc(nm) + '</span>' +
                '<span class="file-meta">' + sizeStr + '</span>' +
                '<span class="file-meta">' + (f.mtime ? App.fmtTime(f.mtime) : '') + '</span>' +
                '<a class="btn btn-muted btn-sm" href="' + dl + '">Download</a>' +
                sendBtn +
                delBtn +
            '</div>';
        }).join('');

        el.querySelectorAll('[data-del]').forEach(function(btn) {
            btn.onclick = async function() {
                var name = btn.getAttribute('data-del');
                if (!await App.confirm('Delete ' + name + '?')) return;
                try {
                    await App.post('/api/loot/delete', {kind: self._sub, name: name});
                    App.toast('Deleted', 'success');
                    self.refresh();
                } catch (e) { App.toast('Failed: ' + e, 'error'); }
            };
        });
        el.querySelectorAll('[data-send]').forEach(function(btn) {
            btn.onclick = async function() {
                var name = btn.getAttribute('data-send');
                if (!await App.confirm('Upload ' + name + ' to WiGLE?')) return;
                try {
                    var r = await App.post('/api/wardrive/upload_wigle', {name: name});
                    App.toast('Uploaded: ' + (r.message || r.status), 'success');
                } catch (e) { App.toast('Failed: ' + e, 'error'); }
            };
        });
        if (kind === 'wigle') this.bindWigleUpload();
    },

    bindWigleUpload() {
        var self = this;
        var btn = document.getElementById('wigle-upload-btn');
        if (!btn) return;
        btn.onclick = async function() {
            var fileInput = document.getElementById('wigle-upload-file');
            var nameInput = document.getElementById('wigle-upload-name');
            if (!fileInput.files[0]) {
                App.toast('Pick a CSV file first', 'error');
                return;
            }
            var file = fileInput.files[0];
            var fname = nameInput.value.trim() || file.name;
            try {
                var resp = await fetch('/api/wardrive/upload_wigle', {
                    method: 'POST',
                    headers: {
                        'X-Pager-Filename': fname,
                        'Content-Type': 'application/octet-stream',
                    },
                    body: await file.arrayBuffer(),
                });
                if (!resp.ok) throw new Error('HTTP ' + resp.status);
                var j = await resp.json();
                App.toast('Uploaded: ' + (j.message || j.status), 'success');
                self.refresh();
            } catch (e) {
                App.toast('Upload failed: ' + e, 'error');
            }
        };
    }
};

App.registerTab('loot', LootTab);
