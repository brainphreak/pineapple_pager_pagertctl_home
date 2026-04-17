/* ========================================
   Terminal Tab - Web Command Execution
   ======================================== */
'use strict';

var TerminalTab = {
    history: [],
    historyIndex: -1,
    output: null,
    input: null,

    cwd: '/mmc/root',

    init() {
        var panel = document.getElementById('tab-terminal');
        panel.innerHTML = '<div class="terminal-panel">' +
            '<div class="terminal-output" id="term-output">' +
            '<div class="text-muted">Pager Terminal - commands execute on the device.</div>' +
            '<div class="text-muted mb-8">Working directory: <span id="term-cwd">/mmc/root</span></div>' +
            '</div>' +
            '<div class="terminal-input-row">' +
            '<span class="terminal-prompt" id="term-prompt">$</span>' +
            '<input class="terminal-input" id="term-input" type="text" placeholder="Enter command..." autocomplete="off" spellcheck="false">' +
            '<button class="btn btn-gold btn-sm" id="term-send">Run</button>' +
            '</div></div>';

        this.output = document.getElementById('term-output');
        this.input = document.getElementById('term-input');

        try {
            this.history = JSON.parse(sessionStorage.getItem('pagerctl_term_history') || '[]');
        } catch (e) { this.history = []; }
        var savedCwd = sessionStorage.getItem('pagerctl_term_cwd');
        if (savedCwd) {
            this.cwd = savedCwd;
            var cwdEl = document.getElementById('term-cwd');
            if (cwdEl) cwdEl.textContent = savedCwd;
        }

        this.input.addEventListener('keydown', e => {
            if (e.key === 'Enter') {
                this.execute();
            } else if (e.key === 'ArrowUp') {
                e.preventDefault();
                this.navigateHistory(-1);
            } else if (e.key === 'ArrowDown') {
                e.preventDefault();
                this.navigateHistory(1);
            }
        });

        document.getElementById('term-send').addEventListener('click', () => this.execute());
    },

    activate() {
        setTimeout(() => this.input.focus(), 100);
    },

    deactivate() {},

    async execute() {
        var cmd = this.input.value.trim();
        if (!cmd) return;

        // Add to history
        if (!this.history.length || this.history[this.history.length - 1] !== cmd) {
            this.history.push(cmd);
            if (this.history.length > 100) this.history.shift();
            sessionStorage.setItem('pagerctl_term_history', JSON.stringify(this.history));
        }

        // Handle `cd` locally so cwd persists across commands
        var cdMatch = cmd.match(/^\s*cd(?:\s+(.*))?$/);
        if (cdMatch) {
            var target = (cdMatch[1] || '/mmc/root').trim();
            if (target.startsWith('~')) target = '/mmc/root' + target.slice(1);
            if (!target.startsWith('/')) {
                target = this.cwd.replace(/\/$/, '') + '/' + target;
            }
            // Ask the server to resolve + check existence
            try {
                var rs = await App.post('/api/terminal', { command: 'cd ' + JSON.stringify(target) + ' && pwd', cwd: this.cwd });
                var newCwd = (rs.output || '').trim().split('\n').pop();
                if (rs.exit_code === 0 && newCwd) {
                    this.cwd = newCwd;
                    sessionStorage.setItem('pagerctl_term_cwd', newCwd);
                    var cwdEl = document.getElementById('term-cwd');
                    if (cwdEl) cwdEl.textContent = newCwd;
                    var promptEl = document.getElementById('term-prompt');
                    if (promptEl) promptEl.textContent = newCwd + ' $';
                } else {
                    var errDiv = document.createElement('div');
                    errDiv.className = 'terminal-result error';
                    errDiv.textContent = rs.output || 'cd failed';
                    this.output.appendChild(errDiv);
                }
            } catch (e) {
                var errDiv2 = document.createElement('div');
                errDiv2.className = 'terminal-result error';
                errDiv2.textContent = 'cd error: ' + e.message;
                this.output.appendChild(errDiv2);
            }
            this.historyIndex = -1;
            this.input.value = '';
            var cmdDivCd = document.createElement('div');
            cmdDivCd.className = 'terminal-cmd';
            cmdDivCd.textContent = cmd;
            // insert cmdDiv BEFORE errDiv if it exists — easier to just append
            this.output.appendChild(cmdDivCd);
            this.output.scrollTop = this.output.scrollHeight;
            return;
        }
        this.historyIndex = -1;
        this.input.value = '';

        // Show command in output
        var cmdDiv = document.createElement('div');
        cmdDiv.className = 'terminal-cmd';
        cmdDiv.textContent = cmd;
        this.output.appendChild(cmdDiv);

        // Disable input while running
        this.input.disabled = true;

        try {
            var result = await App.post('/api/terminal', { command: cmd, cwd: this.cwd });
            var resDiv = document.createElement('div');
            resDiv.className = 'terminal-result' + (result.exit_code !== 0 ? ' error' : '');
            resDiv.textContent = result.output || '(no output)';
            if (result.exit_code !== 0) {
                resDiv.textContent += '\n[exit code: ' + result.exit_code + ']';
            }
            this.output.appendChild(resDiv);
        } catch (e) {
            var errDiv = document.createElement('div');
            errDiv.className = 'terminal-result error';
            errDiv.textContent = 'Error: ' + e.message;
            this.output.appendChild(errDiv);
        }

        this.input.disabled = false;
        this.input.focus();
        this.output.scrollTop = this.output.scrollHeight;
    },

    navigateHistory(dir) {
        if (!this.history.length) return;
        if (this.historyIndex === -1) {
            if (dir === -1) this.historyIndex = this.history.length - 1;
            else return;
        } else {
            this.historyIndex += dir;
        }

        if (this.historyIndex < 0) this.historyIndex = 0;
        if (this.historyIndex >= this.history.length) {
            this.historyIndex = -1;
            this.input.value = '';
            return;
        }

        this.input.value = this.history[this.historyIndex];
    }
};

App.registerTab('terminal', TerminalTab);
