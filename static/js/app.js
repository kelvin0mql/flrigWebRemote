// Debug logging for mobile
function dbg(msg) {
  console.log(msg);
  const debugEl = document.getElementById('debugLog');
  if (debugEl) {
    const time = new Date().toISOString().substr(11, 12);
    debugEl.innerHTML += `[${time}] ${msg}<br>`;
    debugEl.scrollTop = debugEl.scrollHeight;
  }
}

// Initialize Socket.IO connection
const socket = io();

let audioContext = null;

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

// Band dropdown
const bandSelect = document.getElementById('band-select');

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

// Wire CW buttons
function wireCWButtons() {
  document.querySelectorAll('.cw-btn').forEach(btn => {
    btn.addEventListener('click', function() {
      const message = this.getAttribute('data-message');
      socket.emit('send_cw', { message: message });
    });
  });

  document.querySelectorAll('.cw-speed-btn').forEach(btn => {
    btn.addEventListener('click', function() {
      const wpm = parseInt(this.getAttribute('data-wpm'), 10);
      socket.emit('set_cw_speed', { wpm: wpm });
    });
  });
}

function attachAudioDebug() {
  if (!rxAudioEl || rxAudioEl._dbgAttached) return;
  rxAudioEl._dbgAttached = true;
  rxAudioEl.addEventListener('playing', () => dbg(`[audio] playing, readyState=${rxAudioEl.readyState}`));
  rxAudioEl.addEventListener('waiting', () => dbg(`[audio] waiting, readyState=${rxAudioEl.readyState}`));
  rxAudioEl.addEventListener('stalled', () => dbg(`[audio] stalled, networkState=${rxAudioEl.networkState}`));
  rxAudioEl.addEventListener('error', () => dbg(`[audio] error ${rxAudioEl.error}`));
  rxAudioEl.addEventListener('ended', () => dbg('[audio] ended'));
}

// --- WebRTC connect/disconnect ---
async function startWebRTC() {
  if (!rxAudioEl) return;
  if (webrtcConnected) return;

  // Ensure audio context is running
  if (!audioContext) {
    audioContext = new (window.AudioContext || window.webkitAudioContext)();
  }
  if (audioContext.state === 'suspended') {
    await audioContext.resume();
    console.log('[audio] resumed audio context');
  }

  attachAudioDebug();

  pc = new RTCPeerConnection({
    iceServers: [],
    bundlePolicy: 'max-bundle',
  });

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
    dbg(`[webrtc] ontrack: kind=${event.track.kind} id=${event.track.id} readyState=${event.track.readyState} muted=${event.track.muted}`);
    if (event.track.kind === 'audio') {
      const inboundStream = event.streams[0] || new MediaStream([event.track]);
      dbg(`[webrtc] inboundStream tracks=${inboundStream.getTracks().length} active=${inboundStream.active}`);

      event.track.onmute = () => dbg('[webrtc] inbound audio track muted');
      event.track.onended = () => dbg('[webrtc] inbound audio track ended');

      // Wait for track to unmute before playing
      event.track.onunmute = () => {
        dbg('[webrtc] inbound audio track unmuted');

        rxAudioEl.srcObject = inboundStream;
        rxAudioEl.muted = false;
        dbg(`[webrtc] set srcObject, rxAudioEl.readyState=${rxAudioEl.readyState}`);

        // Wait for audio element to have enough data
        const tryPlay = () => {
          dbg(`[webrtc] tryPlay called, readyState=${rxAudioEl.readyState}`);
          const p = rxAudioEl.play();
          if (p && p.then) {
            p.then(() => dbg('[webrtc] audio play() OK')).catch(err => dbg(`[webrtc] audio play() rejected: ${err.message}`));
          }
        };

        if (rxAudioEl.readyState >= 2) {
          // Already have enough data
          tryPlay();
        } else {
          // Wait for canplay event
          rxAudioEl.addEventListener('canplay', tryPlay, { once: true });
        }
      };

      // If already unmuted, play immediately
      if (!event.track.muted) {
        dbg('[webrtc] track already unmuted, playing now');
        rxAudioEl.srcObject = inboundStream;
        rxAudioEl.muted = false;
        dbg(`[webrtc] set srcObject, rxAudioEl.readyState=${rxAudioEl.readyState}`);

        const tryPlay = () => {
          dbg(`[webrtc] tryPlay called, readyState=${rxAudioEl.readyState}`);
          const p = rxAudioEl.play();
          if (p && p.then) {
            p.then(() => dbg('[webrtc] audio play() OK')).catch(err => dbg(`[webrtc] audio play() rejected: ${err.message}`));
          }
        };

        if (rxAudioEl.readyState >= 2) {
          tryPlay();
        } else {
          rxAudioEl.addEventListener('canplay', tryPlay, { once: true });
        }
      }
    }
  };

  // Receive rig audio
  pc.addTransceiver('audio', { direction: 'recvonly' });

  // Set low latency hint on receivers
  try {
    pc.getReceivers().forEach(r => {
      if (typeof r.playoutDelayHint !== 'undefined') r.playoutDelayHint = 0.08;
    });
  } catch (_) {}

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

// Listen for CW sent confirmation
socket.on('cw_sent', function(data) {
  if (data.success) {
    console.log('CW sent:', data.message);
  } else {
    console.error('CW error:', data.error);
    alert('CW Error: ' + data.error);
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

  pwrValue.textContent = data.power;
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

    // Mute local playout during TX
    if (rxAudioEl) {
      rxAudioEl.muted = true;
    }
  } else {
    pttBtn.className = 'btn btn-success';
    pttBtn.style.backgroundColor = '#28a745';
    pttBtn.style.color = 'white';
    socket.emit('ptt_control', { action: 'off' });

    // Flush RX audio buffers to prevent stale audio playback
    flushRXAudioBuffers();

    // Unmute playout after flushing
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

function flushRXAudioBuffers() {
  if (!rxAudioEl) return;

  try {
    // Method 1: Pause and reset currentTime to flush buffer
    rxAudioEl.pause();
    rxAudioEl.currentTime = 0;

    // Method 2: If srcObject exists, recreate the media element binding
    if (rxAudioEl.srcObject) {
      const stream = rxAudioEl.srcObject;
      rxAudioEl.srcObject = null;

      // Small delay to ensure buffers are cleared
      setTimeout(() => {
        rxAudioEl.srcObject = stream;
        rxAudioEl.play().catch(err => {
          dbg(`[audio] Resume play after flush failed: ${err.message}`);
        });
      }, 50);
    }

    dbg('[audio] RX buffers flushed');
  } catch (err) {
    dbg(`[audio] Error flushing buffers: ${err.message}`);
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

// Mic UX: separate connect/disconnect buttons
window.micEnabled = false;
let micStream = null;

function updateMicButtons() {
  const enableBtn = document.getElementById('enable-mic');
  const disableBtn = document.getElementById('disable-mic');
  if (!enableBtn || !disableBtn) return;

  if (window.micEnabled) {
    enableBtn.disabled = true;
    enableBtn.className = 'btn btn-secondary';
    disableBtn.disabled = false;
    disableBtn.className = 'btn btn-danger';
  } else {
    enableBtn.disabled = false;
    enableBtn.className = 'btn btn-outline-secondary';
    disableBtn.disabled = true;
    disableBtn.className = 'btn btn-outline-secondary';
  }
}

async function connectMic() {
  if (window.micEnabled) return;

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

    dbg('[mic] Requesting microphone access...');
    micStream = await navigator.mediaDevices.getUserMedia(constraints);
    dbg('[mic] Got microphone stream');

    // If WebRTC is already connected, add the track and renegotiate
    if (pc && webrtcConnected) {
      const micTrack = micStream.getAudioTracks()[0];
      if (micTrack) {
        pc.addTrack(micTrack, micStream);
        dbg('[mic] Added mic track to PC, renegotiating...');

        // Renegotiate
        const offer = await pc.createOffer();
        await pc.setLocalDescription(offer);

        const res = await fetch('/api/webrtc/offer', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            sdp: pc.localDescription.sdp,
            type: pc.localDescription.type
          })
        });

        if (res.ok) {
          const answer = await res.json();
          await pc.setRemoteDescription(answer);
          dbg('[mic] Renegotiation complete');
        } else {
          const errorText = await res.text();
          dbg(`[mic] Renegotiation failed: ${res.status} ${errorText}`);
        }
      }
    } else {
      dbg('[mic] WebRTC not connected, mic will be added when WebRTC connects');
    }

    window.micEnabled = true;
    updateMicButtons();
    return true;
  } catch (err) {
    dbg(`[mic] getUserMedia failed: ${err.name} - ${err.message}`);
    alert(`Microphone access denied or failed: ${err.message}`);
    window.micEnabled = false;
    updateMicButtons();
    return false;
  }
}

async function disconnectMic() {
  if (!window.micEnabled) return;

  // Stop all mic tracks
  if (micStream) {
    micStream.getTracks().forEach(t => {
      try {
        t.stop();
        dbg('[mic] Stopped mic track');
      } catch (_) {}
    });
    micStream = null;
  }

  // Remove mic tracks from PC if connected
  if (pc && webrtcConnected) {
    const senders = pc.getSenders();
    senders.forEach(sender => {
      if (sender.track && sender.track.kind === 'audio') {
        pc.removeTrack(sender);
        dbg('[mic] Removed mic track from PC');
      }
    });
  }

  window.micEnabled = false;
  updateMicButtons();
}

// Wire up mic buttons
document.addEventListener('DOMContentLoaded', function() {
  const enableMicBtn = document.getElementById('enable-mic');
  const disableMicBtn = document.getElementById('disable-mic');

  if (enableMicBtn) {
    enableMicBtn.addEventListener('click', async () => {
      await connectMic();
    });
  }

  if (disableMicBtn) {
    disableMicBtn.addEventListener('click', async () => {
      await disconnectMic();
    });
  }

  // Initialize mic button states
  updateMicButtons();

  // Initialize PTT button to RX state (green)
  pttBtn.className = 'btn btn-success';
  pttBtn.style.backgroundColor = '#28a745';
  pttBtn.style.color = 'white';

  // Wire up other UI elements
  wireBandButtons();
  wireExtrasA();
  wireCWButtons();

  // Mode dropdown handler
  if (modeSelect) {
    modeSelect.addEventListener('change', () => {
      const m = modeSelect.value;
      if (MODE_SUBSET.includes(m)) {
        socket.emit('set_mode', { mode: m });
      }
    });
  }

  // Band dropdown handler
  if (bandSelect) {
    bandSelect.addEventListener('change', function() {
      const band = this.value;
      socket.emit('band_select', { band: band });
    });
  }

});
