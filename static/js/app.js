/* The editor screen: tap to record, text lands at the caret, copy it out. */

(function () {
    'use strict';

    const DRAFT_KEY = 'pywhispr.draft';
    const FALLBACK_MAX_SECONDS = 300;

    const editor = document.getElementById('editor');
    const recordBtn = document.getElementById('record');
    const copyBtn = document.getElementById('copy');
    const clearBtn = document.getElementById('clear');
    const caption = document.getElementById('record-caption');
    const banner = document.getElementById('banner');
    const status = document.getElementById('status');
    const statusText = document.getElementById('status-text');

    let maxSeconds = FALLBACK_MAX_SECONDS;
    let timerId = null;
    let autoStopId = null;
    let busy = false;

    /* -- chrome ----------------------------------------------------------- */

    function setBanner(kind, message, detail) {
        if (!message) {
            banner.hidden = true;
            return;
        }
        banner.className = 'banner ' + kind;
        banner.textContent = message;
        if (detail) {
            const span = document.createElement('span');
            span.className = 'detail';
            span.textContent = detail;
            banner.appendChild(span);
        }
        banner.hidden = false;
    }

    function setStatus(state, text) {
        status.dataset.state = state;
        statusText.textContent = text;
    }

    function setState(state) {
        document.body.dataset.state = state;
        recordBtn.setAttribute('aria-label', state === 'recording' ? 'Stop recording' : 'Start recording');
        recordBtn.innerHTML = ICONS[state] || ICONS.idle;
    }

    const ICONS = {
        idle: '<span class="level-ring"></span><svg viewBox="0 0 24 24" fill="currentColor" aria-hidden="true">' +
              '<path d="M12 15a3.5 3.5 0 0 0 3.5-3.5v-5a3.5 3.5 0 0 0-7 0v5A3.5 3.5 0 0 0 12 15z"/>' +
              '<path d="M18 11.5a1 1 0 1 0-2 0 4 4 0 0 1-8 0 1 1 0 1 0-2 0 6 6 0 0 0 5 5.91V20a1 1 0 1 0 2 0v-2.59a6 6 0 0 0 5-5.91z"/>' +
              '</svg>',
        recording: '<span class="level-ring"></span><svg viewBox="0 0 24 24" fill="currentColor" aria-hidden="true">' +
                   '<rect x="7" y="7" width="10" height="10" rx="2"/></svg>',
        transcribing: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" aria-hidden="true" class="spin">' +
                      '<path d="M12 3a9 9 0 1 0 9 9" stroke-linecap="round"/></svg>',
    };

    function setLevel(rms) {
        // sqrt curve, because speech RMS sits low and a linear ring barely moves.
        const scaled = Math.min(1, Math.sqrt(rms) * 2.6);
        recordBtn.style.setProperty('--level', scaled.toFixed(3));
    }

    function formatClock(seconds) {
        const whole = Math.floor(seconds);
        return Math.floor(whole / 60) + ':' + String(whole % 60).padStart(2, '0');
    }

    /* -- draft ------------------------------------------------------------- */

    function saveDraft() {
        try { localStorage.setItem(DRAFT_KEY, editor.value); } catch (e) { /* private mode */ }
    }

    function restoreDraft() {
        try {
            const saved = localStorage.getItem(DRAFT_KEY);
            if (saved) { editor.value = saved; }
        } catch (e) { /* private mode */ }
    }

    /* -- text insertion ---------------------------------------------------- */

    /* Insert at the caret, mirroring what PyWhispr does on the desktop (it
     * pastes at the cursor), rather than only ever appending. */
    function insertAtCaret(text) {
        const trimmed = text.trim();
        if (!trimmed) { return; }

        const start = editor.selectionStart;
        const end = editor.selectionEnd;
        const before = editor.value.slice(0, start);
        const after = editor.value.slice(end);

        // Space it sensibly, so dictating twice doesn't run words together and
        // doesn't double up a space that is already there.
        let insertion = trimmed;
        if (before && !/\s$/.test(before)) { insertion = ' ' + insertion; }
        if (after && !/^\s/.test(after)) { insertion = insertion + ' '; }

        if (editor.setRangeText) {
            editor.setRangeText(insertion, start, end, 'end');
        } else {
            editor.value = before + insertion + after;
            editor.selectionStart = editor.selectionEnd = before.length + insertion.length;
        }
        saveDraft();
    }

    /* -- readiness --------------------------------------------------------- */

    function describeVerdicts(verdicts) {
        if (!verdicts || !verdicts.length) { return 'Add a server in settings.'; }
        return verdicts.map(function (v) {
            return (v.name || v.url) + ': ' + (v.error || 'unavailable');
        }).join(' · ');
    }

    async function refreshReadiness() {
        setStatus('checking', 'Checking…');
        try {
            const response = await fetch('/api/ready');
            const data = await response.json();

            if (data.ready) {
                maxSeconds = data.max_audio_seconds || FALLBACK_MAX_SECONDS;
                setStatus('ok', data.server.name);
                status.title = (data.backend || '') + ' at ' + data.server.url;
                recordBtn.disabled = false;
                setBanner(null);
            } else {
                setStatus('bad', 'No server');
                recordBtn.disabled = true;
                setBanner('error', data.message || 'No PyWhispr server is available.',
                          describeVerdicts(data.servers));
            }
        } catch (err) {
            setStatus('bad', 'Offline');
            recordBtn.disabled = true;
            setBanner('error', 'Could not reach pywhispr-web.', String(err.message || err));
        }
    }

    /* -- recording --------------------------------------------------------- */

    function startTimer() {
        timerId = setInterval(function () {
            caption.textContent = formatClock(Recorder.secondsRecorded()) + ' / ' + formatClock(maxSeconds);
        }, 200);
        // The server rejects anything past its own cap, so stop before we waste
        // the user's time uploading something it will refuse.
        autoStopId = setTimeout(function () {
            setBanner('warn', 'Reached the ' + maxSeconds + " second limit, transcribing what we've got.");
            toggleRecording();
        }, maxSeconds * 1000);
    }

    function stopTimer() {
        clearInterval(timerId);
        clearTimeout(autoStopId);
        timerId = autoStopId = null;
    }

    async function beginRecording() {
        if (!Recorder.isSupported()) {
            setBanner('error', 'This browser will not give us the microphone.',
                      window.isSecureContext
                          ? 'Web Audio is unavailable here.'
                          : 'Microphone access needs HTTPS (or localhost). Serve this app over HTTPS and reload.');
            return;
        }

        try {
            await Recorder.start(setLevel);
        } catch (err) {
            const denied = err && (err.name === 'NotAllowedError' || err.name === 'SecurityError');
            setBanner('error',
                      denied ? 'Microphone permission was denied.' : 'Could not start recording.',
                      denied ? 'Allow microphone access for this site, then try again.' : String(err.message || err));
            return;
        }

        setBanner(null);
        setState('recording');
        caption.textContent = '0:00 / ' + formatClock(maxSeconds);
        startTimer();
    }

    async function finishRecording() {
        stopTimer();
        setState('transcribing');
        caption.textContent = 'Transcribing…';
        setLevel(0);

        let clip;
        try {
            clip = await Recorder.stop();
        } catch (err) {
            setState('idle');
            caption.textContent = '';
            setBanner('error', 'Could not process the recording.', String(err.message || err));
            return;
        }

        if (!clip || clip.pcm.length === 0) {
            setState('idle');
            caption.textContent = '';
            setBanner('warn', 'That recording was empty.', 'Check the right microphone is selected.');
            return;
        }

        await sendForTranscription(clip);
        setState('idle');
        caption.textContent = '';
    }

    async function sendForTranscription(clip, isRetry) {
        const query = '?sample_rate=' + clip.sampleRate + '&channels=1&format=s16le';
        let response;
        try {
            // A typed array (not a stream) so the browser sets Content-Length:
            // PyWhispr rejects chunked bodies with HTTP 411.
            response = await fetch('/api/transcribe' + query, {
                method: 'POST',
                headers: {'Content-Type': 'application/octet-stream'},
                body: clip.pcm,
            });
        } catch (err) {
            setBanner('error', 'Upload failed.', String(err.message || err));
            return;
        }

        let data = {};
        try { data = await response.json(); } catch (e) { /* handled below */ }

        if (response.ok && typeof data.text === 'string') {
            if (data.text.trim()) {
                insertAtCaret(data.text);
                setBanner(null);
            } else {
                setBanner('warn', 'No speech was recognised in that recording.');
            }
            if (data.server) { setStatus('ok', data.server.name); }
            return;
        }

        const code = (data.error && data.error.code) || 'error';
        const message = (data.error && data.error.message) || 'Transcription failed.';

        // The model is still warming up. Honour Retry-After and try once more,
        // rather than making the user re-record.
        if (code === 'model_loading' && !isRetry) {
            const wait = parseInt(response.headers.get('Retry-After') || '5', 10);
            setBanner('info', 'The server is still loading its model, retrying in ' + wait + 's…');
            await new Promise(function (resolve) { setTimeout(resolve, wait * 1000); });
            return sendForTranscription(clip, true);
        }

        setBanner('error', FRIENDLY[code] || message, FRIENDLY[code] ? message : describeVerdicts(data.servers));
        if (code === 'no_server' || code === 'server_unreachable') { refreshReadiness(); }
    }

    const FRIENDLY = {
        no_server: 'No PyWhispr server is available.',
        server_unreachable: 'The server went away mid-request.',
        model_loading: 'The server is still loading its model.',
        model_unavailable: 'The server could not load its model.',
        busy: 'The server is busy with other requests.',
        payload_too_large: 'That recording is too long for the server.',
        bad_audio: 'The server could not read that audio.',
        timeout: 'The server took too long to transcribe.',
        unsupported_media_type: 'The server rejected the audio format.',
    };

    let toggling = false;

    async function toggleRecording() {
        // Double-taps on a phone are easy; make the second one a no-op rather
        // than a second overlapping recording.
        if (toggling || busy) { return; }
        toggling = true;
        try {
            if (Recorder.isRecording()) {
                busy = true;
                recordBtn.disabled = true;
                try {
                    await finishRecording();
                } finally {
                    busy = false;
                    recordBtn.disabled = false;
                }
            } else {
                await beginRecording();
            }
        } finally {
            toggling = false;
        }
    }

    /* -- copy -------------------------------------------------------------- */

    async function copyText() {
        const text = editor.value;
        if (!text) {
            setBanner('warn', 'Nothing to copy yet.');
            return;
        }

        try {
            if (navigator.clipboard && window.isSecureContext) {
                await navigator.clipboard.writeText(text);
            } else {
                // execCommand is the only option on older iOS and over plain
                // HTTP, where navigator.clipboard is absent.
                editor.focus();
                editor.setSelectionRange(0, text.length);
                if (!document.execCommand('copy')) { throw new Error('copy command was rejected'); }
                editor.setSelectionRange(text.length, text.length);
            }
            setBanner('ok', 'Copied to the clipboard.');
            setTimeout(function () {
                if (banner.classList.contains('ok')) { setBanner(null); }
            }, 2000);
        } catch (err) {
            setBanner('error', 'Could not copy automatically.', 'Select the text and copy it manually.');
        }
    }

    function clearText() {
        if (!editor.value || window.confirm('Clear the editor?')) {
            editor.value = '';
            saveDraft();
            editor.focus();
        }
    }

    /* -- wiring ------------------------------------------------------------ */

    recordBtn.addEventListener('click', toggleRecording);
    copyBtn.addEventListener('click', copyText);
    clearBtn.addEventListener('click', clearText);
    editor.addEventListener('input', saveDraft);

    // Coming back to a backgrounded phone app, the cached server may be long
    // gone; re-check rather than showing stale state.
    document.addEventListener('visibilitychange', function () {
        if (!document.hidden && !Recorder.isRecording() && !busy) { refreshReadiness(); }
    });

    setState('idle');
    restoreDraft();
    refreshReadiness();

    if (!window.isSecureContext) {
        setBanner('warn', 'Microphone access needs HTTPS.',
                  'Browsers only allow recording on HTTPS or localhost. Recording will fail until this app is served over HTTPS.');
    }
})();
