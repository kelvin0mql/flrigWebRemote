// Initialize Socket.IO connection
const socket = io();

// DOM elements
const connectionStatus = document.getElementById('connection-status');
const lastUpdate = document.getElementById('last-update');
const frequencyA = document.getElementById('frequency-a');
const frequencyB = document.getElementById('frequency-b');
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

// Current frequency for digit manipulation
let currentFrequencyHz = 0;

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
  frequencyB.textContent = data.frequency_b;

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

function updateClickableFrequency(freqMHz) {
  // Store current frequency for manipulation
  currentFrequencyHz = parseFloat(freqMHz) * 1e6;

  // Format frequency with individual clickable digits
  const freqStr = parseFloat(freqMHz).toFixed(3);
  const parts = freqStr.split('.');
  const integerPart = parts[0].padStart(4, '0'); // Ensure 4 digits
  const decimalPart = parts[1];

  let html = '';

  // Integer part (MHz)
  for (let i = 0; i < integerPart.length; i++) {
    const digit = integerPart[i];
    const digitValue = Math.pow(10, 6 + (3 - i)); // Power for MHz
    html += `<span class="digit" data-value="${digitValue}" data-digit="${digit}">${digit}</span>`;
  }

  html += '<span class="digit">.</span>'; // Decimal point

  // Decimal part (kHz)
  for (let i = 0; i < decimalPart.length; i++) {
    const digit = decimalPart[i];
    const digitValue = Math.pow(10, 3 - i - 1) * 1000; // Power for kHz
    html += `<span class="digit" data-value="${digitValue}" data-digit="${digit}">${digit}</span>`;
  }

  frequencyA.innerHTML = html;

  // Add click handlers to digits
  const digitElements = frequencyA.querySelectorAll('.digit[data-value]');
  digitElements.forEach(digitEl => {
    digitEl.addEventListener('click', handleDigitClick);
  });
}

function handleDigitClick(event) {
  const digitEl = event.target;
  const digitValue = parseInt(digitEl.getAttribute('data-value'));
  const currentDigit = parseInt(digitEl.getAttribute('data-digit'));

  // Determine if click was on upper or lower half
  const rect = digitEl.getBoundingClientRect();
  const clickY = event.clientY - rect.top;
  const isUpperHalf = clickY < (rect.height / 2);

  let newFrequency = currentFrequencyHz;

  if (isUpperHalf) {
    // Increment digit
    if (currentDigit < 9) {
      newFrequency += digitValue;
    } else {
      // Roll over: remove 9, add 0 (net effect: subtract 9 * digitValue)
      newFrequency -= 9 * digitValue;
    }
  } else {
    // Decrement digit
    if (currentDigit > 0) {
      newFrequency -= digitValue;
    } else {
      // Roll under: remove 0, add 9 (net effect: add 9 * digitValue)
      newFrequency += 9 * digitValue;
    }
  }

  // Bounds checking
  if (newFrequency < 1000000) newFrequency = 1000000; // 1 MHz minimum
  if (newFrequency > 60000000) newFrequency = 60000000; // 60 MHz maximum

  // Send frequency change to server
  sendFrequencyChange(newFrequency);

  // Visual feedback
  digitEl.classList.add('active');
  setTimeout(() => {
    digitEl.classList.remove('active');
  }, 150);
}

function sendFrequencyChange(frequencyHz) {
  // Send via WebSocket to server
  socket.emit('frequency_change', {
    frequency: frequencyHz,
    vfo: 'A'
  });
}

// Handle frequency change responses
socket.on('frequency_changed', function(data) {
  if (data.success) {
    console.log('Frequency changed successfully');
  } else {
    console.error('Failed to change frequency:', data.error);
  }
});

// Initial load - fetch current status
fetch('/api/status')
  .then(response => response.json())
  .then(data => updateDisplay(data))
  .catch(error => console.error('Error fetching initial status:', error));
