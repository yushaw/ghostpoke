const { app, BrowserWindow, ipcMain } = require('electron');
const http = require('http');
const path = require('path');
const fs = require('fs');

const LOG_PATH = '/tmp/electron-echo-events.jsonl';
const counters = { click: 0, move: 0, scroll: 0, key: 0, drag: 0 };

function isAppFocused() {
  return BrowserWindow.getFocusedWindow() !== null;
}

function appendEvent(type, extra = {}) {
  const payload = {
    ts: new Date().toISOString(),
    type,
    pid: process.pid,
    clickCount: counters.click,
    counters: { ...counters },
    appIsFocused: isAppFocused(),
    ...extra,
  };
  fs.appendFileSync(LOG_PATH, JSON.stringify(payload) + '\n', 'utf8');
}

function createWindow() {
  const win = new BrowserWindow({
    width: 600,
    height: 500,
    show: true,
    backgroundColor: '#1a1a2e',
    title: 'Electron Echo',
    webPreferences: {
      preload: path.join(__dirname, 'preload.js'),
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: false,
    },
  });

  win.loadFile('index.html');

  win.on('focus', () => appendEvent('window-focus'));
  win.on('blur', () => appendEvent('window-blur'));
  win.webContents.on('did-finish-load', () => {
    appendEvent('window-ready', { windowId: win.id });
    startCdpServer(win);
  });
}

const CDP_PORT = 17321;

function startCdpServer(win) {
  try {
    win.webContents.debugger.attach('1.3');
  } catch (err) {
    appendEvent('cdp-error', { error: String(err) });
    return;
  }

  const server = http.createServer((req, res) => {
    if (req.method !== 'POST' || req.url !== '/dispatch') {
      res.writeHead(404);
      res.end('');
      return;
    }
    let body = '';
    req.on('data', (chunk) => { body += chunk; });
    req.on('end', async () => {
      try {
        const params = JSON.parse(body);
        await win.webContents.debugger.sendCommand(
          'Input.dispatchMouseEvent', params,
        );
        res.writeHead(200, { 'Content-Type': 'application/json' });
        res.end(JSON.stringify({ ok: true }));
      } catch (err) {
        res.writeHead(500, { 'Content-Type': 'application/json' });
        res.end(JSON.stringify({ ok: false, error: String(err) }));
      }
    });
  });

  server.listen(CDP_PORT, '127.0.0.1', () => {
    appendEvent('cdp-ready', { port: CDP_PORT });
  });
}

app.on('browser-window-focus', () => appendEvent('app-focus'));
app.on('browser-window-blur', () => appendEvent('app-blur'));

ipcMain.handle('echo:clicked', (_event, payload = {}) => {
  counters.click += 1;
  appendEvent('clicked', { rendererData: payload });
  return { counters };
});

ipcMain.handle('echo:moved', (_event, payload = {}) => {
  counters.move += 1;
  appendEvent('moved', { rendererData: payload });
  return { counters };
});

ipcMain.handle('echo:scrolled', (_event, payload = {}) => {
  counters.scroll += 1;
  appendEvent('scrolled', { rendererData: payload });
  return { counters };
});

ipcMain.handle('echo:keyPressed', (_event, payload = {}) => {
  counters.key += 1;
  appendEvent('keyPressed', { rendererData: payload });
  return { counters };
});

ipcMain.handle('echo:dragged', (_event, payload = {}) => {
  counters.drag += 1;
  appendEvent('dragged', { rendererData: payload });
  return { counters };
});

app.whenReady().then(() => {
  try {
    fs.writeFileSync(LOG_PATH, '', 'utf8');
  } catch {}
  appendEvent('app-ready');
  createWindow();
});

app.on('window-all-closed', () => {
  appendEvent('window-all-closed');
  app.quit();
});
