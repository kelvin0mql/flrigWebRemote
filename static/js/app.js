// Initialize Socket.IO connection
const socket = io();

// DOM elements
const connectionStatus = document.getElementById('connection-status');
const lastUpdate = document.getElementById('last-update');
const frequencyA = document.getElementById('frequency-a');
const currentMode = document.getElementById('current-mode');
const currentBandwidth = document.getElementById('current-bandwidth');
const micValue = document.getElementById('mic-value');
const powerValue = document.getElementById('power-value');
const rfValue = document.getElementById('rf-value');
const volumeValue = document.getElementById('volume-value');
const swrValue = document.getElementById('swr-value');
const micSlider = document.getElementById('mic-slider');
const powerSlider = document.getElementById('power-slider');
const rfSlider = document.getElementById('rf-slider');
const volumeSlider = document.getElementById('volume-slider');

// Control buttons
const tuneBtn = document.getElementById('tune-btn');
const pttBtn = document.getElementById('ptt-btn');

// Live audio elements (may or may not exist depending on the page)
const rxAudioEl = document.getElementById('rx-audio');
// const audioToggleBtn = document.getElementById('audio-toggle'); // removed
const listenWavBtn = document.getElementById('listen-wav');
const listenMp3Btn = document.getElementById('listen-mp3');

let currentStreamKind = null; // 'wav' | 'mp3' | null

// Current frequency for digit manipulation
let currentFrequencyHz = 0;
let pttActive = false;
let tuneActive = false;

// Track audio state related to PTT
let audioMutedByPTT = false;
let audioPrevVolume = 1.0;
let audioWasListeningBeforePTT = false;

// --- WebRTC state ---
let pc = null;
let webrtcConnected = false;

// Helpers to control the live stream from here (independent of the UI button text)
function isListening() {
  // repurposed to reflect WebRTC connection state
  return webrtcConnected;
}

function updateListenButtons() {
  if (!listenWavBtn || !listenMp3Btn) return;
  // Repurpose UI: left button = Connect, right button = Disconnect
  if (webrtcConnected) {
    listenWavBtn.textContent = 'Connected (Opus)';
    listenWavBtn.className = 'btn btn-secondary';
    listenMp3Btn.textContent = 'Disconnect';
    listenMp3Btn.className = 'btn btn-outline-danger';
  } else {
    listenWavBtn.textContent = 'Connect Audio';
    listenWavBtn.className = 'btn btn-outline-secondary';
    listenMp3Btn.textContent = 'Disconnect';
    listenMp3Btn.className = 'btn btn-outline-secondary';
  }
}

function stopLiveAudioStream() {
  // replaced by stopWebRTC()
  stopWebRTC();
  currentStreamKind = null;
  updateListenButtons();
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

// --- New: WebRTC connect/disconnect ---
async function startWebRTC() {
  if (!rxAudioEl) return;
  if (webrtcConnected) return;

  attachAudioDebug();

  pc = new RTCPeerConnection({
    iceServers: [], // local LAN, no STUN/TURN needed
    bundlePolicy: 'max-bundle',
  });

  // Low-latency hints (supported best on Chromium; Safari will still behave well)
  try {
    pc.getReceivers().forEach(r => {
      if (typeof r.playoutDelayHint !== 'undefined') r.playoutDelayHint = 0.08; // ~80ms
    });
  } catch (_) {}

  pc.onicecandidate = (e) => {
    if (!e.candidate) {
      // Final candidate gathering complete; nothing special to do in trickle-less mode
    }
  };

  pc.ontrack = (event) => {
    console.log('[webrtc] ontrack: kind=', event.track.kind);
    if (event.track.kind === 'audio') {
      const inboundStream = event.streams[0] || new MediaStream([event.track]);
      rxAudioEl.srcObject = inboundStream;
      rxAudioEl.muted = false;
      const p = rxAudioEl.play();
      if (p && p.catch) {
        p.catch(err => console.warn('[webrtc] audio play() rejected:', err));
      }
    }
  };

  // Ask to receive only (server will add rig RX track)
  pc.addTransceiver('audio', { direction: 'recvonly' });

  // Create offer
  const offer = await pc.createOffer({
    offerToReceiveAudio: true,
    offerToReceiveVideo: false
  });
  await pc.setLocalDescription(offer);

  // Send SDP to server, get answer
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
  await pc.setRemoteDescription(answer);
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
  } finally {
    pc = null;
    webrtcConnected = false;
    updateListenButtons();
    console.log('[webrtc] disconnected');
  }
}

function startLiveAudioStream(kind) {
  // legacy HTTP streaming removed in favor of WebRTC
  console.log('[audio] legacy HTTP streaming disabled; using WebRTC');
}

// Wire up the two listen buttons
if (listenWavBtn) {
  listenWavBtn.addEventListener('click', () => {
    if (!webrtcConnected) {
      startWebRTC();
    }
  });
}
if (listenMp3Btn) {
  listenMp3Btn.addEventListener('click', () => {
    if (webrtcConnected) {
      stopWebRTC();
    }
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

function updateDisplay(data) {
  // Update connection status
  if (data.connected) {
    connectionStatus.textContent = 'Connected';
    connectionStatus.className = 'badge bg-success';
  } else {
    connectionStatus.textContent = 'Disconnected';
    connectionStatus.className = 'badge bg-danger';
  }

  // Update last update time
  lastUpdate.textContent = `Last update: ${data.last_update}`;

  // Update frequency displays with clickable digits
  updateClickableFrequency(data.frequency_a);

  // Update mode and bandwidth
  currentMode.textContent = data.mode;
  currentBandwidth.textContent = data.bandwidth;

  // Update control values
  micValue.textContent = data.mic_gain;
  micSlider.value = data.mic_gain;

  powerValue.textContent = data.power;
  powerSlider.value = data.power;

  rfValue.textContent = data.rf_gain;
  rfSlider.value = data.rf_gain;

  volumeValue.textContent = data.volume;
  volumeSlider.value = data.volume;

  // Update SWR
  swrValue.textContent = data.swr.toFixed(1);

  // Color-code SWR based on value
  if (data.swr > 2.0) {
    swrValue.style.color = '#dc3545'; // Red
  } else if (data.swr > 1.5) {
    swrValue.style.color = '#ffc107'; // Yellow
  } else {
    swrValue.style.color = '#28a745'; // Green
  }
}

function updateClickableFrequency(freqKHz) {
  // Store current frequency for manipulation - convert kHz back to Hz
  currentFrequencyHz = parseFloat(freqKHz) * 1e3;

  // Format frequency as kHz.XX (like flrig: 7200.00)
  const freqStr = parseFloat(freqKHz).toFixed(2);
  const parts = freqStr.split('.');
  let integerPart = parts[0]; // This is now the kHz part
  const decimalPart = parts[1]; // This is now hundredths of kHz (10Hz steps)

  // Remove leading zeros but keep at least one digit
  integerPart = integerPart.replace(/^0+/, '') || '0';

  let html = '';

  // Calculate the digit powers based on the actual number of digits shown
  const numDigits = integerPart.length;

  // Integer part (kHz) - no leading zeros
  for (let i = 0; i < integerPart.length; i++) {
    const digit = integerPart[i];
    const digitPower = numDigits - 1 - i; // Position from right (0-based)
    const digitValue = Math.pow(10, digitPower + 3); // +3 for kHz to Hz conversion
    html += `<span class="digit clickable-digit" data-value="${digitValue}" data-digit="${digit}">${digit}</span>`;
  }

  html += '<span class="digit">.</span>'; // Decimal point

  // Decimal part (hundredths of kHz = 10Hz steps)
  for (let i = 0; i < decimalPart.length; i++) {
    const digit = decimalPart[i];
    const digitValue = Math.pow(10, 2 - i - 1) * 10; // Power for 10Hz steps
    html += `<span class="digit clickable-digit" data-value="${digitValue}" data-digit="${digit}">${digit}</span>`;
  }

  frequencyA.innerHTML = html;

  // Set up event listeners only once using event delegation
  if (!frequencyListenerSetup) {
    console.log('Setting up frequency event listeners'); // Debug

    // Use event delegation on the parent container
    frequencyA.addEventListener('click', function(event) {
      console.log('Frequency click detected'); // Debug
      handleDigitInteraction(event.target, event);
    });

    frequencyA.addEventListener('touchend', function(event) {
      console.log('Frequency touch detected'); // Debug
      event.preventDefault();
      handleDigitInteraction(event.target, event.changedTouches[0]);
    });

    frequencyListenerSetup = true;
  }
}

function handleDigitInteraction(digitEl, eventData) {
  // Check if the clicked element is a clickable digit
  if (!digitEl.classList.contains('clickable-digit')) {
    console.log('Not a clickable digit:', digitEl.className); // Debug
    return;
  }

  const digitValue = parseInt(digitEl.getAttribute('data-value'));
  const currentDigit = parseInt(digitEl.getAttribute('data-digit'));

  console.log(`*** DIGIT CLICK DETECTED ***`); // Make this very obvious
  console.log(`Clicked digit: ${currentDigit}, value: ${digitValue}Hz`);

  // Determine if click was on upper or lower half
  const rect = digitEl.getBoundingClientRect();
  const clickY = eventData.clientY - rect.top;
  const isUpperHalf = clickY < (rect.height / 2);

  console.log(`Click Y: ${clickY}, Height: ${rect.height}, Upper half: ${isUpperHalf}`);

  let newFrequency = currentFrequencyHz;

  if (isUpperHalf) {
    // Increment digit - proper carry logic
    newFrequency += digitValue;
  } else {
    // Decrement digit - proper borrow logic
    newFrequency -= digitValue;
  }

  console.log(`*** SENDING FREQUENCY CHANGE ***`);
  console.log(`Old frequency: ${currentFrequencyHz}Hz, New frequency: ${newFrequency}Hz`);

  // Bounds checking
  if (newFrequency < 1000000) newFrequency = 1000000; // 1 MHz minimum
  if (newFrequency > 60000000) newFrequency = 60000000; // 60 MHz maximum

  // IMMEDIATE local update for responsive UX
  currentFrequencyHz = newFrequency;
  updateLocalFrequencyDisplay(newFrequency);

  // Send to server in background
  sendFrequencyChange(newFrequency);

  // Visual feedback
  digitEl.classList.add('active');
  setTimeout(() => {
    digitEl.classList.remove('active');
  }, 150);
}

function updateLocalFrequencyDisplay(frequencyHz) {
  // Convert Hz to kHz and update display immediately
  const freqKHz = frequencyHz / 1e3;
  const freqStr = freqKHz.toFixed(2);
  const parts = freqStr.split('.');
  let integerPart = parts[0];
  const decimalPart = parts[1];

  integerPart = integerPart.replace(/^0+/, '') || '0';

  let html = '';
  const numDigits = integerPart.length;

  // Integer part
  for (let i = 0; i < integerPart.length; i++) {
    const digit = integerPart[i];
    const digitPower = numDigits - 1 - i;
    const digitValue = Math.pow(10, digitPower + 3);
    html += `<span class="digit clickable-digit" data-value="${digitValue}" data-digit="${digit}">${digit}</span>`;
  }

  html += '<span class="digit">.</span>';

  // Decimal part
  for (let i = 0; i < decimalPart.length; i++) {
    const digit = decimalPart[i];
    const digitValue = Math.pow(10, 2 - i - 1) * 10;
    html += `<span class="digit clickable-digit" data-value="${digitValue}" data-digit="${digit}">${digit}</span>`;
  }

  frequencyA.innerHTML = html;
}

function sendFrequencyChange(frequencyHz) {
  socket.emit('frequency_change', {
    frequency: frequencyHz,
    vfo: 'A'
  });
}

// Tune button handler
tuneBtn.addEventListener('click', function() {
  tuneActive = !tuneActive;

  if (tuneActive) {
    tuneBtn.className = 'btn btn-warning me-3';
    socket.emit('tune_control', { action: 'start' });

    // Auto-reset after 10 seconds since we can't reliably detect when tuning is complete
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

// PTT button handler - changed to single click toggle
pttBtn.addEventListener('click', function() {
  togglePTT();
});

function togglePTT() {
  // Capture listening state BEFORE toggling
  audioWasListeningBeforePTT = isListening();

  pttActive = !pttActive;

  if (pttActive) {
    // TX mode - solid red background
    pttBtn.className = 'btn btn-danger';
    pttBtn.style.backgroundColor = '#dc3545';
    pttBtn.style.color = 'white';
    socket.emit('ptt_control', { action: 'on' });

    // Immediately silence (mute + volume 0) and stop the stream to prevent echo
    if (rxAudioEl) {
      if (!rxAudioEl.muted) {
        audioPrevVolume = rxAudioEl.volume;
        rxAudioEl.muted = true;
        rxAudioEl.volume = 0;
        audioMutedByPTT = true;
      }
      if (isListening()) {
        stopLiveAudioStream();
      }
    }
  } else {
    // RX mode - solid green background
    pttBtn.className = 'btn btn-success';
    pttBtn.style.backgroundColor = '#28a745';
    pttBtn.style.color = 'white';
    socket.emit('ptt_control', { action: 'off' });

    // Restore only what we changed for TX
    if (rxAudioEl) {
      if (audioMutedByPTT) {
        rxAudioEl.muted = false;
        rxAudioEl.volume = audioPrevVolume || 1.0;
        audioMutedByPTT = false;
      }
      if (audioWasListeningBeforePTT && currentStreamKind) {
        startLiveAudioStream(currentStreamKind);
      } else {
        updateListenButtons();
      }
    }
  }
}

function deactivatePTT() {
  if (pttActive) {
    pttActive = false;
    // RX mode - solid green background
    pttBtn.className = 'btn btn-success';
    pttBtn.style.backgroundColor = '#28a745';
    pttBtn.style.color = 'white';
    socket.emit('ptt_control', { action: 'off' });

    if (rxAudioEl) {
      if (audioMutedByPTT) {
        rxAudioEl.muted = false;
        rxAudioEl.volume = audioPrevVolume || 1.0;
        audioMutedByPTT = false;
      }
      if (audioWasListeningBeforePTT && currentStreamKind) {
        startLiveAudioStream(currentStreamKind);
      } else {
        updateListenButtons();
      }
    }
  }
}

// Initialize buttons state on load
document.addEventListener('DOMContentLoaded', updateListenButtons);

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
    // Reset tune button on error
    tuneActive = false;
    tuneBtn.className = 'btn btn-outline-info me-3';
  }
});

socket.on('ptt_response', function(data) {
  if (!data.success) {
    console.error('PTT command failed:', data.error);
    // Reset PTT button on error
    deactivatePTT();
  }
});

// Initial load - fetch current status
fetch('/api/status')
  .then(response => response.json())
  .then(data => updateDisplay(data))
  .catch(error => console.error('Error fetching initial status:', error));

// Initialize PTT button to RX state (green) after DOM is loaded
document.addEventListener('DOMContentLoaded', function() {
  // Initialize PTT button to RX state (green)
  pttBtn.className = 'btn btn-success';
  pttBtn.style.backgroundColor = '#28a745';
  pttBtn.style.color = 'white';
});
