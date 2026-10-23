'use strict';

const byId = id => document.getElementById(id);

function status(id, message, kind = '') {
  const element = byId(id);
  element.textContent = message;
  element.className = `status ${kind}`.trim();
}

async function getData(path) {
  const response = await fetch(path, { cache: 'no-store' });
  if (!response.ok) throw new Error(`HTTP ${response.status}`);
  return response.json();
}

function display(value) {
  return value === null || value === undefined || value === '' ? '—' : String(value);
}

function uptime(seconds) {
  const total = Number(seconds);
  if (!Number.isFinite(total) || total < 0) return '—';
  const days = Math.floor(total / 86400);
  const hours = Math.floor(total % 86400 / 3600);
  const minutes = Math.floor(total % 3600 / 60);
  return [days && `${days} 天`, (days || hours) && `${hours} 小时`, `${minutes} 分钟`].filter(Boolean).join(' ');
}

function versionText(version) {
  if (version === null || version === undefined) return '—';
  if (typeof version !== 'object') return display(version);
  const rom = version.rom || version.ROM;
  const system = version.rootfs || version.linux;
  if (rom || system) return [...new Set([system, rom].filter(Boolean))].join(' · ');
  const entries = Object.entries(version).filter(([, value]) => value !== null && value !== undefined && value !== '' && typeof value !== 'object');
  return entries.length ? entries.map(([key, value]) => `${key}: ${value}`).join(' · ') : '—';
}

async function loadDevice() {
  try {
    const { data } = await getData('/device/info');
    if (!data || typeof data !== 'object') throw new Error('invalid data');
    if (data.hostname) byId('hostname').textContent = String(data.hostname);
    byId('device-hostname').textContent = display(data.hostname);
    byId('device-ip').textContent = display(data.ip);
    byId('device-model').textContent = display(data.model);
    byId('device-uptime').textContent = uptime(data.uptime);
    byId('device-version').textContent = versionText(data.version);
    status('device-status', '设备信息已更新', 'success');
  } catch (_) {
    status('device-status', '无法读取设备信息，请稍后刷新页面重试。', 'error');
  }
}

const mixerLabels = { mysoftvol: '主音量', 'headphone volume': '主音量', notifyvol: '通知音量', bluetooth: '蓝牙音量' };

function volumeRow(item) {
  const { mixer, volume } = item;
  const row = document.createElement('div');
  row.className = 'volume-row';
  const top = document.createElement('div');
  top.className = 'volume-top';
  const label = document.createElement('strong');
  label.textContent = mixerLabels[mixer] || display(item.name || mixer);
  const value = document.createElement('span');
  value.textContent = `${volume}%`;
  top.append(label, value);
  const slider = document.createElement('input');
  slider.type = 'range';
  slider.min = '0'; slider.max = '100'; slider.step = '1'; slider.value = String(volume);
  slider.setAttribute('aria-label', `${label.textContent}，目标音量`);
  slider.addEventListener('input', () => { value.textContent = `${slider.value}%`; });
  const actions = document.createElement('div');
  actions.className = 'volume-actions';
  const message = document.createElement('span');
  message.className = 'status';
  message.setAttribute('role', 'status');
  message.textContent = `当前 ${volume}%`;
  const button = document.createElement('button');
  button.type = 'button';
  button.textContent = '应用';
  button.addEventListener('click', async () => {
    button.disabled = true;
    message.className = 'status';
    message.textContent = '正在应用…';
    try {
      const body = new URLSearchParams({ mixer, volume: slider.value });
      const response = await fetch('/volume', { method: 'POST', headers: { 'Content-Type': 'application/x-www-form-urlencoded' }, body });
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      const { data } = await getData('/volume');
      if (!Array.isArray(data)) throw new Error('invalid data');
      const actual = data.find(entry => entry.mixer === mixer);
      if (!actual || !Number.isInteger(Number(actual.volume))) throw new Error('missing readback');
      slider.value = String(actual.volume);
      value.textContent = `${actual.volume}%`;
      message.textContent = `已应用，当前 ${actual.volume}%`;
      message.className = 'status success';
    } catch (_) {
      message.textContent = '应用失败或无法读取实际音量，请稍后重试。';
      message.className = 'status error';
    } finally {
      button.disabled = false;
    }
  });
  actions.append(message, button);
  row.append(top, slider, actions);
  return row;
}

async function loadVolumes() {
  try {
    const { data } = await getData('/volume');
    if (!Array.isArray(data)) throw new Error('invalid data');
    const list = byId('volume-list');
    list.replaceChildren();
    for (const item of data) {
      if (!item || typeof item.mixer !== 'string' || !Number.isInteger(Number(item.volume)) || Number(item.volume) < 0 || Number(item.volume) > 100) continue;
      list.appendChild(volumeRow(item));
    }
    status('volume-status', list.childElementCount ? '' : '设备未返回可用的音量控制。');
  } catch (_) {
    status('volume-status', '无法读取音量，请稍后刷新页面重试。', 'error');
  }
}

function instanceCard(instance) {
  const card = document.createElement('div');
  card.className = 'instance';
  const details = document.createElement('div');
  const name = document.createElement('strong');
  name.textContent = display(instance.location_name || instance.base_url);
  const url = document.createElement('p');
  url.textContent = [instance.base_url, instance.version].filter(Boolean).join(' · ');
  details.append(name, url);
  const form = document.createElement('form');
  form.action = '/auth'; form.method = 'post';
  const input = document.createElement('input');
  input.type = 'hidden'; input.name = 'url'; input.value = instance.base_url;
  const button = document.createElement('button');
  button.type = 'submit'; button.textContent = '连接';
  form.append(input, button);
  card.append(details, form);
  return card;
}

async function loadDiscovery() {
  try {
    const result = await getData('/discover');
    if (!Array.isArray(result.instances)) throw new Error('invalid data');
    const list = byId('instance-list');
    list.replaceChildren();
    for (const instance of result.instances) {
      if (instance && typeof instance.base_url === 'string' && /^https?:\/\//i.test(instance.base_url)) list.appendChild(instanceCard(instance));
    }
    if (result.hostname && byId('device-hostname').textContent === '—') byId('hostname').textContent = String(result.hostname);
    status('discover-status', list.childElementCount ? `发现 ${list.childElementCount} 个 Home Assistant 实例。` : '未发现 Home Assistant 实例；可以在下方手动输入地址。');
  } catch (_) {
    status('discover-status', '局域网发现失败；仍可在下方手动输入 Home Assistant 地址。', 'error');
  }
}

function authenticated(value) {
  return value === true || value === 1 || value === '1' || value === 'true' || value === 'True';
}

async function loadWakewords(current) {
  try {
    const result = await getData('/config/wakewords');
    const words = result.data && result.data.wakewords;
    if (!Array.isArray(words)) throw new Error('invalid data');
    const select = byId('word');
    select.replaceChildren(new Option('请选择唤醒词', ''));
    for (const word of words) if (typeof word === 'string') select.add(new Option(word, word));
    if (current && !words.includes(current)) select.add(new Option(current, current));
    select.value = current || '';
    status('wakeword-status', words.length ? '' : '设备未返回可用唤醒词。');
  } catch (_) {
    status('wakeword-status', '无法读取唤醒词，请稍后刷新页面重试。', 'error');
  }
}

async function loadConfig() {
  try {
    const result = await getData('/config');
    const listener = result.data && result.data.listener;
    if (!listener || typeof listener !== 'object') throw new Error('invalid data');
    if (!authenticated(listener.HA_AUTH_SETUP)) {
      status('config-status', '尚未连接 Home Assistant；本地音箱功能仍可使用。');
      return;
    }
    byId('config-section').hidden = false;
    status('config-status', 'Home Assistant 已连接。', 'success');
    for (const name of ['ha_stt_provider', 'stt_language']) byId(name).value = listener[name.toUpperCase()] || '';
    const tts = result.data.tts;
    byId('tts_language').value = (tts && tts.LANGUAGE) || listener.TTS_LANGUAGE || '';
    await loadWakewords(listener.WORD || '');
  } catch (_) {
    status('config-status', '无法读取 Home Assistant 配置状态。', 'error');
  }
}

document.addEventListener('DOMContentLoaded', () => {
  loadDevice();
  loadVolumes();
  loadDiscovery();
  loadConfig();
});
