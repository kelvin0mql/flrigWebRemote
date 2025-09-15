// Initialize Socket.IO connection
const socket = io();

// Forward selected console output to server (debug.log) and optionally mute browser console
(() => {
  const ECHO_IN_BROWSER = false;           // set true to also see logs in DevTools
  const TAGS = ['[webrtc]', '[audio]'];    // only forward lines containing these tags

  function stringify(parts) {
    return parts.map(p => {
      if (p instanceof Error) return (p.stack || String(p));
      if (typeof p === 'object') {
        try { return JSON.stringify(p); } catch (_) { return String(p); }
      }
      return String(p);
    }).join(' ');
  }

  function shouldForward(parts) {
    const s = stringify(parts);
    return TAGS.some(t => s.includes(t));
  }

  ['log','warn','error'].forEach(level => {
    const orig = console[level].bind(console);
    console[level] = (...args) => {
      if (shouldForward(args)) {
        try { socket.emit('client_debug', { level, msg: stringify(args) }); } catch (_) {}
      }
      if (ECHO_IN_BROWSER) orig(...args);
    };
  });
})();

// DOM elements (must match index.html)
const connectionStatus = document.getElementById('connection-status');
const lastUpdate = document.getElementById('last-update');
const frequencyA = document.getElementById('frequency-a');

const pwrValue = document.getElementById('pwr-value');
const swrValue = document.getElementById('swr-value');

// Control buttons
const tuneBtn = document.getElementById('tune-btn');
const pttBtn = document.getElementById('ptt-btn');

// Live audio elements (WebRTC)
const rxAudioEl = document.getElementById('rx-audio');
const connectAudioBtn = document.getElementById('connect-audio');
const disconnectAudioBtn = document.getElementById('disconnect-audio');

// Mode dropdown (compact subset)
const modeSelect = document.getElementById('mode-select');
const MODE_SUBSET = ['LSB','USB','CW','AM','PKT-L','PKT-U'];

// Frequency limits (Hz)
const MIN_FREQ_HZ = 30000;      // 30 kHz: typical HF rig lower limit
const MAX_FREQ_HZ = 56000000;   // 56 MHz: your rig's upper limit

// Current frequency for digit manipulation
let currentFrequencyHz = MIN_FREQ_HZ;
let pttActive = false;
let tuneActive = false;
// Remember if RX audio was connected before PTT
let wasListeningBeforePTT = false;

// WebRTC state
let pc = null;
let webrtcConnected = false;

function isListening() {
  return webrtcConnected;
}

function updateListenButtons() {
  if (!connectAudioBtn || !disconnectAudioBtn) return;
  if (webrtcConnected) {
    connectAudioBtn.textContent = 'Connected (Opus)';
    connectAudioBtn.className = 'btn btn-secondary';
    disconnectAudioBtn.textContent = 'Disconnect';
    disconnectAudioBtn.className = 'btn btn-outline-danger';
  } else {
    connectAudioBtn.textContent = 'Connect Audio';
    connectAudioBtn.className = 'btn btn-outline-secondary';
    disconnectAudioBtn.textContent = 'Disconnect';
    disconnectAudioBtn.className = 'btn btn-outline-secondary';
  }
}

function stopLiveAudioStream() {
  stopWebRTC();
  updateListenButtons();
}

// Wire Mode dropdown -> emit to server
document.addEventListener('DOMContentLoaded', function() {
  if (modeSelect) {
    modeSelect.addEventListener('change', () => {
      const m = modeSelect.value;
      if (MODE_SUBSET.includes(m)) {
        socket.emit('set_mode', { mode: m });
      }
    });
  }
});

// Wire Band buttons (click => emit band_select)
function wireBandButtons() {
  document.querySelectorAll('.band-buttons .band-btn').forEach(btn => {
    btn.addEventListener('click', () => {
      const band = btn.getAttribute('data-band');
      if (!band) return;
      socket.emit('band_select', { band });
    });
  });
}

// Wire Extras (A) user buttons -> emit user_button with cmd index (1..8)
function wireExtrasA() {
  const container = document.querySelector('.extras-a');
  if (!container) return;
  container.querySelectorAll('.extras-a-btn').forEach(btn => {
    btn.addEventListener('click', () => {
      const cmd = parseInt(btn.getAttribute('data-cmd'), 10);
      if (!Number.isInteger(cmd)) return;
      socket.emit('user_button', { cmd });
    });
  });
}

function attachAudioDebug() {
  if (!rxAudioEl || rxAudioEl._dbgAttached) return;
  rxAudioEl._dbgAttached = true;
  rxAudioEl.addEventListener('playing', () => console.log('[audio] playing, readyState=', rxAudioEl.readyState));
  rxAudioEl.addEventListener('waiting', () => console.log('[audio] waiting, readyState=', rxAudioEl.readyState));
  rxAudioEl.addEventListener('stalled', () => console.warn('[audio] stalled, networkState=', rxAudioEl.networkState));
  rxAudioEl.addEventListener('error', () => console.error('[audio] error', rxAudioEl.error));
  rxAudioEl.addEventListener('ended', () => console.log('[audio] ended'));
}

// --- WebRTC connect/disconnect ---
async function startWebRTC() {
  if (!rxAudioEl) return;
  if (webrtcConnected) return;

  attachAudioDebug();

  pc = new RTCPeerConnection({
    iceServers: [],
    bundlePolicy: 'max-bundle',
  });

  try {
    pc.getReceivers().forEach(r => {
      if (typeof r.playoutDelayHint !== 'undefined') r.playoutDelayHint = 0.08;
    });
  } catch (_) {}

  pc.onconnectionstatechange = () => {
    console.log('[webrtc] pc.connectionState =', pc.connectionState);
  };
  pc.oniceconnectionstatechange = () => {
    console.log('[webrtc] pc.iceConnectionState =', pc.iceConnectionState);
  };
  pc.onicegatheringstatechange = () => {
    console.log('[webrtc] pc.iceGatheringState =', pc.iceGatheringState);
  };

  pc.onicecandidate = (e) => { /* trickle-less; nothing extra needed */ };

  pc.ontrack = (event) => {
    console.log('[webrtc] ontrack: kind=', event.track.kind, 'id=', event.track.id);
    if (event.track.kind === 'audio') {
      const inboundStream = event.streams[0] || new MediaStream([event.track]);
      event.track.onmute = () => console.warn('[webrtc] inbound audio track muted');
      event.track.onunmute = () => console.log('[webrtc] inbound audio track unmuted');
      event.track.onended = () => console.warn('[webrtc] inbound audio track ended');

      rxAudioEl.srcObject = inboundStream;
      rxAudioEl.muted = false;

      const p = rxAudioEl.play();
      if (p && p.then) {
        p.then(() => console.log('[webrtc] audio play() OK')).catch(err => console.warn('[webrtc] audio play() rejected:', err));
      }
    }
  };

  // Receive rig audio
  pc.addTransceiver('audio', { direction: 'recvonly' });

  // If mic was enabled before connect, add a send track now
  try {
    if (window.micEnabled && typeof micStream !== 'undefined' && micStream) {
      const micTrack = micStream.getAudioTracks && micStream.getAudioTracks()[0];
      if (micTrack) {
        pc.addTrack(micTrack);
        console.log('[webrtc] added mic track to PC');
      }
    }
  } catch (e) {
    console.warn('[webrtc] failed to attach mic track:', e);
  }

  const offer = await pc.createOffer({
    offerToReceiveAudio: true,
    offerToReceiveVideo: false
  });
  await pc.setLocalDescription(offer);
  console.log('[webrtc] created offer, sdp bytes=', (pc.localDescription.sdp || '').length);

  const res = await fetch('/api/webrtc/offer', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      sdp: pc.localDescription.sdp,
      type: pc.localDescription.type
    })
  });

  if (!res.ok) {
    console.error('[webrtc] offer failed:', res.status, await res.text());
    await stopWebRTC();
    return;
  }

  const answer = await res.json();
  console.log('[webrtc] received answer, sdp bytes=', (answer && answer.sdp ? answer.sdp.length : -1));
  await pc.setRemoteDescription(answer);

  try {
    const recv = pc.getReceivers().find(r => r.track && r.track.kind === 'audio');
    if (recv && recv.track) {
      console.log('[webrtc] receiver track readyState=', recv.track.readyState);
    } else {
      console.warn('[webrtc] no audio receiver track found after setRemoteDescription');
    }
  } catch (e) {
    console.warn('[webrtc] receiver inspect failed:', e);
  }

  webrtcConnected = true;
  updateListenButtons();
  console.log('[webrtc] connected');
}

async function stopWebRTC() {
  try {
    if (rxAudioEl) {
      try { rxAudioEl.pause(); } catch (_) {}
      rxAudioEl.srcObject = null;
      rxAudioEl.removeAttribute('src');
      rxAudioEl.load();
    }
    if (pc) {
      pc.getSenders().forEach(s => { try { s.track && s.track.stop(); } catch (_) {} });
      pc.getReceivers().forEach(r => { try { r.track && r.track.stop(); } catch (_) {} });
      pc.ontrack = null;
      pc.onicecandidate = null;
      try { pc.close(); } catch (_) {}
    }
    try {
      await fetch('/api/webrtc/teardown', { method: 'POST' });
    } catch (_) {}
  } finally {
    pc = null;
    webrtcConnected = false;
    updateListenButtons();
    console.log('[webrtc] disconnected');
  }
}

// Wire up audio connect/disconnect buttons
if (connectAudioBtn) {
  connectAudioBtn.addEventListener('click', () => {
    if (!webrtcConnected) startWebRTC();
  });
}
if (disconnectAudioBtn) {
  disconnectAudioBtn.addEventListener('click', () => {
    if (webrtcConnected) stopWebRTC();
  });
}

// Global event listener setup - only do this once
let frequencyListenerSetup = false;

// Handle status updates from server
socket.on('status_update', function(data) {
  updateDisplay(data);
});

// Handle connection events
socket.on('connect', function() {
  console.log('Connected to server');
});

socket.on('disconnect', function() {
  console.log('Disconnected from server');
  connectionStatus.textContent = 'Disconnected';
  connectionStatus.className = 'badge bg-danger';
});

// Optional acknowledgement logging from server when mode is set
socket.on('mode_set', (data) => {
  if (!data || !data.success) {
    console.warn('[mode] set failed:', data && data.error);
  }
});

function updateDisplay(data) {
  // Connection status
  if (data.connected) {
    connectionStatus.textContent = 'Connected';
    connectionStatus.className = 'badge bg-success';
  } else {
    connectionStatus.textContent = 'Disconnected';
    connectionStatus.className = 'badge bg-danger';
  }

  // Last update
  lastUpdate.textContent = `Last update: ${data.last_update}`;

  // Frequency (clickable digits)
  updateClickableFrequency(data.frequency_a);

  // Mode (sync dropdown if present; otherwise leave any legacy label alone)
  if (modeSelect) {
    const m = String(data.mode || '').toUpperCase();
    if (MODE_SUBSET.includes(m) && modeSelect.value !== m) {
      modeSelect.value = m;
    }
  }

  // PWR and SWR inline
  pwrValue.textContent = data.power;
  const swr = Number(data.swr || 0);
  swrValue.textContent = swr.toFixed(1);
  swrValue.classList.remove('swr-ok', 'swr-warn', 'swr-bad');
  if (swr > 2.0) {
    swrValue.classList.add('swr-bad');
  } else if (swr > 1.5) {
    swrValue.classList.add('swr-warn');
  } else {
    swrValue.classList.add('swr-ok');
  }
}

function updateClickableFrequency(freqKHz) {
  currentFrequencyHz = parseFloat(freqKHz) * 1e3;

  const freqStr = parseFloat(freqKHz).toFixed(2);
  const parts = freqStr.split('.');
  let integerPart = parts[0];
  const decimalPart = parts[1];

  integerPart = integerPart.replace(/^0+/, '') || '0';

  let html = '';
  const numDigits = integerPart.length;

  // Integer part (kHz)
  for (let i = 0; i < integerPart.length; i++) {
    const digit = integerPart[i];
    const digitPower = numDigits - 1 - i;
    const digitValue = Math.pow(10, digitPower + 3); // position value in Hz
    html += `<span class="digit clickable-digit" data-value="${digitValue}" data-digit="${digit}">${digit}</span>`;
  }
  html += '<span class="digit">.</span>';
  // Decimal part (hundredths of kHz = 10 Hz steps)
  for (let i = 0; i < decimalPart.length; i++) {
    const digit = decimalPart[i];
    const digitValue = Math.pow(10, 2 - i - 1) * 10;
    html += `<span class="digit clickable-digit" data-value="${digitValue}" data-digit="${digit}">${digit}</span>`;
  }

  frequencyA.innerHTML = html;

  // Event listeners only once (delegation on container)
  if (!frequencyListenerSetup) {
    frequencyA.addEventListener('click', function(event) {
      handleDigitInteraction(event.target, event);
    });
    frequencyA.addEventListener('touchend', function(event) {
      event.preventDefault();
      handleDigitInteraction(event.target, event.changedTouches[0]);
    });
    frequencyListenerSetup = true;
  }
}

function handleDigitInteraction(digitEl, eventData) {
  if (!digitEl.classList.contains('clickable-digit')) return;

  const digitValue = parseInt(digitEl.getAttribute('data-value'), 10);
  const rect = digitEl.getBoundingClientRect();
  const clickY = eventData.clientY - rect.top;
  const isUpperHalf = clickY < (rect.height / 2);

  let newFrequency = currentFrequencyHz;
  newFrequency += isUpperHalf ? digitValue : -digitValue;

  // Bounds
  if (newFrequency < 1000000) newFrequency = 1000000;
  if (newFrequency > 60000000) newFrequency = 60000000;

  // Local update for snappy UI
  currentFrequencyHz = newFrequency;
  updateLocalFrequencyDisplay(newFrequency);

  // Send to server
  sendFrequencyChange(newFrequency);

  // Visual feedback
  digitEl.classList.add('active');
  setTimeout(() => digitEl.classList.remove('active'), 150);
}

function updateLocalFrequencyDisplay(frequencyHz) {
  const freqKHz = frequencyHz / 1e3;
  const freqStr = freqKHz.toFixed(2);
  const parts = freqStr.split('.');
  let integerPart = parts[0];
  const decimalPart = parts[1];

  integerPart = integerPart.replace(/^0+/, '') || '0';

  let html = '';
  const numDigits = integerPart.length;

  for (let i = 0; i < integerPart.length; i++) {
    const digit = integerPart[i];
    const digitPower = numDigits - 1 - i;
    const digitValue = Math.pow(10, digitPower + 3);
    html += `<span class="digit clickable-digit" data-value="${digitValue}" data-digit="${digit}">${digit}</span>`;
  }
  html += '<span class="digit">.</span>';
  for (let i = 0; i < decimalPart.length; i++) {
    const digit = decimalPart[i];
    const digitValue = Math.pow(10, 2 - i - 1) * 10;
    html += `<span class="digit clickable-digit" data-value="${digitValue}" data-digit="${digit}">${digit}</span>`;
  }

  frequencyA.innerHTML = html;
}

function sendFrequencyChange(frequencyHz) {
  socket.emit('frequency_change', { frequency: frequencyHz, vfo: 'A' });
}

// Tune button handler (toggle)
tuneBtn.addEventListener('click', function() {
  tuneActive = !tuneActive;

  if (tuneActive) {
    tuneBtn.className = 'btn btn-warning me-3';
    socket.emit('tune_control', { action: 'start' });
    setTimeout(() => {
      if (tuneActive) {
        tuneActive = false;
        tuneBtn.className = 'btn btn-outline-info me-3';
      }
    }, 10000);
  } else {
    tuneBtn.className = 'btn btn-outline-info me-3';
    socket.emit('tune_control', { action: 'stop' });
  }
});

// PTT button handler (toggle)
pttBtn.addEventListener('click', function() {
  togglePTT();
});

function togglePTT() {
  // remember if RX audio was connected before toggling
  wasListeningBeforePTT = isListening();

  pttActive = !pttActive;

  if (pttActive) {
    pttBtn.className = 'btn btn-danger';
    pttBtn.style.backgroundColor = '#dc3545';
    pttBtn.style.color = 'white';
    socket.emit('ptt_control', { action: 'on' });

    // Do NOT disconnect WebRTC anymore; just mute local playout during TX
    if (rxAudioEl) {
      rxAudioEl.muted = true;
    }
  } else {
    pttBtn.className = 'btn btn-success';
    pttBtn.style.backgroundColor = '#28a745';
    pttBtn.style.color = 'white';
    socket.emit('ptt_control', { action: 'off' });

    // Unmute playout; we didn't disconnect the stream
    if (rxAudioEl) {
      rxAudioEl.muted = false;
    }

    // if we were listening before TX and the stream was somehow closed, reconnect
    if (wasListeningBeforePTT && !webrtcConnected) {
      startWebRTC();
    }
    wasListeningBeforePTT = false;

    updateListenButtons();
  }
}

function deactivatePTT() {
  if (!pttActive) return;
  pttActive = false;
  pttBtn.className = 'btn btn-success';
  pttBtn.style.backgroundColor = '#28a745';
  pttBtn.style.color = 'white';
  socket.emit('ptt_control', { action: 'off' });
  updateListenButtons();
}

// Handle server responses
socket.on('frequency_changed', function(data) {
  if (data.success) {
    console.log('Frequency changed successfully');
  } else {
    console.error('Failed to change frequency:', data.error);
  }
});

socket.on('tune_response', function(data) {
  if (!data.success) {
    console.error('Tune command failed:', data.error);
    tuneActive = false;
    tuneBtn.className = 'btn btn-outline-info me-3';
  }
});

socket.on('band_selected', function(data) {
  if (data && data.success) {
    console.log('[band] tuned to', data.band, '=>', data.frequency_hz, 'Hz');
  } else {
    console.warn('[band] tune failed:', data && data.error);
  }
});

// Acknowledgement for Extras (A) user buttons (cmd 1..8)
socket.on('user_button_ack', function(data) {
  if (data && data.success) {
    console.log(`[extrasA] ran user cmd ${data.cmd}`);
  } else {
    console.warn('[extrasA] cmd failed:', data && data.error);
  }
});

socket.on('ptt_response', function(data) {
  if (!data.success) {
    console.error('PTT command failed:', data.error);
    deactivatePTT();
  }
});

// Initialize PTT button to RX state (green) after DOM is loaded
document.addEventListener('DOMContentLoaded', function() {
  pttBtn.className = 'btn btn-success';
  pttBtn.style.backgroundColor = '#28a745';
  pttBtn.style.color = 'white';
});

// Mic UX: compact "Mic" button with enabled/disabled styling
window.micEnabled = false;

function updateMicButton() {
  const btn = document.getElementById('enable-mic');
  if (!btn) return;
  if (window.micEnabled) {
    btn.textContent = 'Mic';
    btn.className = 'btn btn-danger';
    btn.style.backgroundColor = '#dc3545';
    btn.style.color = 'white';
    btn.style.fontWeight = 'bold';
  } else {
    btn.textContent = 'Mic';
    btn.className = 'btn btn-outline-secondary';
    btn.style.backgroundColor = '';
    btn.style.color = '';
    btn.style.fontWeight = '';
  }
}

async function requestMicPermission() {
  try {
    const constraints = {
      audio: {
        echoCancellation: false,
        noiseSuppression: false,
        autoGainControl: false,
        channelCount: 1,
        sampleRate: 48000
      },
      video: false
    };
    const stream = await navigator.mediaDevices.getUserMedia(constraints);
    // We only need the permission now; stop tracks immediately
    stream.getTracks().forEach(t => { try { t.stop(); } catch (_) {} });
    window.micEnabled = true;
    updateMicButton();
    return true;
  } catch (err) {
    console.warn('[mic] getUserMedia failed:', err);
    window.micEnabled = false;
    updateMicButton();
    return false;
  }
}

// Final DOM wiring: Mic button + restore UI wiring (listen buttons, band, extras)
document.addEventListener('DOMContentLoaded', function() {
  const enableMicBtn = document.getElementById('enable-mic');
  if (enableMicBtn) {
    enableMicBtn.addEventListener('click', async () => {
      await requestMicPermission();
    });
  }
  // Ensure Mic button reflects current state
  updateMicButton();
  // Restore the rest of the UI bindings
  updateListenButtons();
  wireBandButtons();
  wireExtrasA();
});

// Keep mic capture alive for the whole session (do NOT stop tracks)
let micStream = null;

// requestMicPermission persists the stream to not kill RX audio
async function requestMicPermission() {
  try {
    // If we already have a live stream, ensure RX playout is active and return
    if (micStream && micStream.getTracks().some(t => t.readyState === 'live')) {
      window.micEnabled = true;
      updateMicButton();
      if (rxAudioEl && rxAudioEl.srcObject) {
        try { await rxAudioEl.play(); } catch (_) {}
      }
      return true;
    }

    const constraints = {
      audio: {
        echoCancellation: false,
        noiseSuppression: false,
        autoGainControl: false,
        channelCount: 1,
        sampleRate: 48000
      },
      video: false
    };

    // Acquire and KEEP the mic stream; do NOT stop its tracks
    micStream = await navigator.mediaDevices.getUserMedia(constraints);
    window.micEnabled = true;
    updateMicButton();

    // If iOS paused RX playout when mic became active, resume it
    if (rxAudioEl && rxAudioEl.srcObject) {
      try { await rxAudioEl.play(); } catch (_) {}
    }

    return true;
  } catch (err) {
    console.warn('[mic] getUserMedia failed:', err);
    window.micEnabled = false;
    updateMicButton();
    return false;
  }
}
