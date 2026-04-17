/* ========================================
   Wardrive Tab - state, stats, networks, controls
   ======================================== */
'use strict';

var WardriveTab = {
    init() {
        var panel = document.getElementById('tab-wardrive');
        panel.innerHTML =
            '<div class="grid grid-cols-2">' +
              '<div class="card card-accent">' +
                '<h3>Status</h3>' +
                '<div class="kv"><span>State</span><span id="wd-state" class="bad">STOPPED</span></div>' +
                '<div class="kv"><span>Interface</span><span id="wd-iface">-</span></div>' +
                '<div class="kv"><span>Channel hop</span><span id="wd-chop">-</span></div>' +
                '<div class="kv"><span>PCAP</span><span id="wd-pcap">-</span></div>' +
                '<div class="kv"><span>Wigle export</span><span id="wd-wigle">-</span></div>' +
                '<div class="btn-row" style="margin-top:10px">' +
                  '<button id="wd-start" class="btn btn-gold">Start</button>' +
                  '<button id="wd-stop" class="btn btn-muted">Stop</button>' +
                  '<button id="wd-clear" class="btn btn-danger">Clear DB</button>' +
                '</div>' +
              '</div>' +
              '<div class="card card-accent">' +
                '<h3>GPS</h3>' +
                '<div class="kv"><span>Device</span><span id="wd-gps-dev">-</span></div>' +
                '<div class="kv"><span>Fix</span><span id="wd-gps-fix">-</span></div>' +
                '<div class="kv"><span>Lat</span><span id="wd-gps-lat">-</span></div>' +
                '<div class="kv"><span>Lon</span><span id="wd-gps-lon">-</span></div>' +
                '<div class="kv"><span>Sats</span><span id="wd-gps-sats">-</span></div>' +
              '</div>' +
              '<div class="card card-accent">' +
                '<h3>Statistics</h3>' +
                '<div class="stat-grid">' +
                  '<div class="stat"><div class="lbl">Total</div><div class="val" id="wd-total">-</div></div>' +
                  '<div class="stat"><div class="lbl">Open</div><div class="val" id="wd-open">-</div></div>' +
                  '<div class="stat"><div class="lbl">WEP</div><div class="val" id="wd-wep">-</div></div>' +
                  '<div class="stat"><div class="lbl">WPA2</div><div class="val" id="wd-wpa">-</div></div>' +
                  '<div class="stat"><div class="lbl">WPA3</div><div class="val" id="wd-wpa3">-</div></div>' +
                  '<div class="stat"><div class="lbl">Handshakes</div><div class="val" id="wd-hs">-</div></div>' +
                '</div>' +
              '</div>' +
              '<div class="card card-accent card-full">' +
                '<h3>Recent Networks</h3>' +
                '<div id="wd-nets" class="file-list"></div>' +
              '</div>' +
            '</div>';
        this.bindEvents();
    },

    bindEvents() {
        var self = this;
        document.getElementById('wd-start').onclick = async () => {
            try { await App.post('/api/wardrive/start'); App.toast('Started', 'success'); }
            catch (e) { App.toast('Start failed: ' + e, 'error'); }
            self.refresh();
        };
        document.getElementById('wd-stop').onclick = async () => {
            try { await App.post('/api/wardrive/stop'); App.toast('Stopped', 'success'); }
            catch (e) { App.toast('Stop failed: ' + e, 'error'); }
            self.refresh();
        };
        document.getElementById('wd-clear').onclick = async () => {
            if (!await App.confirm('Delete all wardrive networks from the DB?')) return;
            await App.post('/api/wardrive/clear');
            App.toast('DB cleared', 'success');
            self.refresh();
        };
    },

    activate() {
        this.refresh();
        App.startPolling('wardrive', () => this.refresh(), 4000);
    },

    deactivate() {},

    setTxt(id, v) {
        var el = document.getElementById(id);
        if (el) el.textContent = (v != null && v !== '') ? v : '-';
    },

    async refresh() {
        try {
            var s = await App.api('/api/wardrive/state');
            var st = document.getElementById('wd-state');
            st.textContent = s.running ? 'RUNNING' : 'STOPPED';
            st.className = s.running ? 'ok' : 'bad';
            var cfg = s.config || {};
            this.setTxt('wd-iface', cfg.capture_interface);
            this.setTxt('wd-chop', cfg.channel_hop ? 'on' : 'off');
            this.setTxt('wd-pcap', cfg.pcap_enabled ? 'on' : 'off');
            this.setTxt('wd-wigle', cfg.wigle_enabled ? 'on' : 'off');
            this.setTxt('wd-gps-dev', cfg.gps_device);
            var g = s.gps || {};
            this.setTxt('wd-gps-fix', g.fix ? 'YES' : 'no');
            this.setTxt('wd-gps-lat', g.lat);
            this.setTxt('wd-gps-lon', g.lon);
            this.setTxt('wd-gps-sats', g.sats);
        } catch (e) {}
        try {
            var st2 = await App.api('/api/wardrive/stats');
            this.setTxt('wd-total', st2.total);
            this.setTxt('wd-open', st2.open);
            this.setTxt('wd-wep', st2.wep);
            this.setTxt('wd-wpa', st2.wpa);
            this.setTxt('wd-wpa3', st2.wpa3);
            this.setTxt('wd-hs', st2.handshakes);
        } catch (e) {}
        try {
            var n = await App.api('/api/wardrive/networks?limit=50');
            var list = n.networks || [];
            var el = document.getElementById('wd-nets');
            if (!list.length) {
                el.innerHTML = '<div class="muted">no networks scanned</div>';
                return;
            }
            el.innerHTML = list.map(function(r) {
                var hs = r.handshake ? ' <span class="ok">HS</span>' : '';
                return '<div class="file-row">' +
                    '<span class="file-name">' + App.esc(r.ssid || '(hidden)') + '</span>' +
                    '<span class="file-meta">' + App.esc(r.bssid) + '</span>' +
                    '<span class="file-meta">' + App.esc(r.encryption) + '</span>' +
                    '<span class="file-meta">ch ' + App.esc(r.channel) + '</span>' +
                    '<span class="file-meta">' + App.esc(r.signal) + ' dBm' + hs + '</span>' +
                '</div>';
            }).join('');
        } catch (e) {}
    }
};

App.registerTab('wardrive', WardriveTab);
