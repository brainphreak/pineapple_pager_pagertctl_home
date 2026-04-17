/* ========================================
   Dashboard Tab - sysinfo + captive/wardrive summary
   ======================================== */
'use strict';

var DashboardTab = {
    init() {
        var panel = document.getElementById('tab-dashboard');
        panel.innerHTML =
            '<div class="grid grid-cols-2">' +
              '<div class="card card-accent">' +
                '<h3>System</h3>' +
                '<div class="stat-grid">' +
                  '<div class="stat"><div class="lbl">CPU</div><div class="val" id="d-cpu">-</div></div>' +
                  '<div class="stat"><div class="lbl">Memory</div><div class="val" id="d-mem">-</div></div>' +
                  '<div class="stat"><div class="lbl">Temp</div><div class="val" id="d-temp">-</div></div>' +
                  '<div class="stat"><div class="lbl">Disk</div><div class="val" id="d-disk">-</div></div>' +
                  '<div class="stat"><div class="lbl">Uptime</div><div class="val" id="d-up">-</div></div>' +
                  '<div class="stat"><div class="lbl">Procs</div><div class="val" id="d-procs">-</div></div>' +
                  '<div class="stat"><div class="lbl">Battery</div><div class="val" id="d-battery">-</div></div>' +
                '</div>' +
              '</div>' +
              '<div class="card card-accent">' +
                '<h3>Identity</h3>' +
                '<div class="kv"><span>Hostname</span><span id="d-host">-</span></div>' +
                '<div class="kv"><span>Kernel</span><span id="d-kernel">-</span></div>' +
                '<h4 style="margin-top:12px">Interfaces</h4>' +
                '<div id="d-ifaces" class="kv-list"></div>' +
                '<h4 style="margin-top:12px">USB</h4>' +
                '<div id="d-usb" class="kv-list"></div>' +
              '</div>' +
              '<div class="card card-accent">' +
                '<h3>Captive Portal</h3>' +
                '<div class="kv"><span>State</span><span id="d-cap-state">-</span></div>' +
                '<div class="kv"><span>SSID</span><span id="d-cap-ssid">-</span></div>' +
                '<div class="kv"><span>Clients</span><span id="d-cap-clients">-</span></div>' +
                '<div class="kv"><span>Creds</span><span id="d-cap-creds">-</span></div>' +
                '<div class="kv"><span>Cached</span><span id="d-cap-cache">-</span></div>' +
                '<div class="kv"><span>Internet</span><span id="d-cap-net">-</span></div>' +
                '<div style="margin-top:10px">' +
                  '<a href="#captive" class="btn btn-gold btn-sm">Manage →</a>' +
                '</div>' +
              '</div>' +
              '<div class="card card-accent">' +
                '<h3>Wardrive</h3>' +
                '<div class="kv"><span>State</span><span id="d-wd-state">-</span></div>' +
                '<div class="kv"><span>APs total</span><span id="d-wd-total">-</span></div>' +
                '<div class="kv"><span>Open</span><span id="d-wd-open">-</span></div>' +
                '<div class="kv"><span>WPA2/3</span><span id="d-wd-wpa">-</span></div>' +
                '<div class="kv"><span>Handshakes</span><span id="d-wd-hs">-</span></div>' +
                '<div class="kv"><span>GPS</span><span id="d-wd-gps">-</span></div>' +
                '<div style="margin-top:10px">' +
                  '<a href="#wardrive" class="btn btn-gold btn-sm">Manage →</a>' +
                '</div>' +
              '</div>' +
            '</div>';
    },

    activate() {
        App.startPolling('dashboard', () => this.refresh(), 3000);
    },

    deactivate() {},

    setTxt(id, val) {
        var el = document.getElementById(id);
        if (el) el.textContent = (val != null && val !== '') ? val : '-';
    },

    async refresh() {
        try {
            var s = await App.api('/api/sysinfo');
            this.setTxt('d-cpu', s.cpu);
            this.setTxt('d-mem', s.mem);
            this.setTxt('d-temp', s.temp);
            this.setTxt('d-disk', s.disk);
            this.setTxt('d-up', s.uptime);
            this.setTxt('d-procs', s.procs);
            this.setTxt('d-battery', s.battery);
            this.setTxt('d-host', s.hostname);
            this.setTxt('d-kernel', s.kernel);

            var ifDiv = document.getElementById('d-ifaces');
            var ifaces = s.interfaces || [];
            ifDiv.innerHTML = ifaces.length ? ifaces.map(function(i) {
                return '<div class="kv"><span>' + App.esc(i[0]) + '</span><span>' + App.esc(i[1]) + '</span></div>';
            }).join('') : '<div class="muted">none</div>';

            var usbDiv = document.getElementById('d-usb');
            var usb = s.usb || [];
            usbDiv.innerHTML = usb.length ? usb.map(function(d) {
                return '<div class="kv"><span>' + App.esc(d) + '</span><span></span></div>';
            }).join('') : '<div class="muted">none</div>';
        } catch (e) {}

        try {
            var c = await App.api('/api/captive/state');
            var running = c.running;
            var stateEl = document.getElementById('d-cap-state');
            stateEl.textContent = running ? 'RUNNING' : 'STOPPED';
            stateEl.className = running ? 'ok' : 'bad';
            this.setTxt('d-cap-ssid', (c.config && c.config.ssid) || '-');
            this.setTxt('d-cap-clients', (c.clients || []).length);
            this.setTxt('d-cap-creds', c.cred_count);
            this.setTxt('d-cap-cache', c.cache_count);
            var netEl = document.getElementById('d-cap-net');
            var blocked = c.internet_blocked;
            netEl.textContent = blocked ? 'BLOCKED' : 'ALLOWED';
            netEl.className = blocked ? 'bad' : 'ok';
        } catch (e) {}

        try {
            var w = await App.api('/api/wardrive/state');
            var st = document.getElementById('d-wd-state');
            st.textContent = w.running ? 'RUNNING' : 'STOPPED';
            st.className = w.running ? 'ok' : 'bad';
            var gps = w.gps || {};
            var gpsText = gps.fix
                ? ('FIX ' + (gps.sats || 0) + ' sats')
                : 'no fix';
            this.setTxt('d-wd-gps', gpsText);
        } catch (e) {}

        try {
            var st2 = await App.api('/api/wardrive/stats');
            this.setTxt('d-wd-total', st2.total);
            this.setTxt('d-wd-open', st2.open);
            this.setTxt('d-wd-wpa', (st2.wpa || 0) + '/' + (st2.wpa3 || 0));
            this.setTxt('d-wd-hs', st2.handshakes);
        } catch (e) {}
    }
};

App.registerTab('dashboard', DashboardTab);
