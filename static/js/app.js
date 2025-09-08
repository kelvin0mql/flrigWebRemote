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

  // Update frequency displays
  frequencyA.textContent = data.frequency_a;
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

// Initial load - fetch current status
fetch('/api/status')
  .then(response => response.json())
  .then(data => updateDisplay(data))
  .catch(error => console.error('Error fetching initial status:', error));
