const { contextBridge, ipcRenderer } = require('electron');

contextBridge.exposeInMainWorld('echoBridge', {
  clicked: (data) => ipcRenderer.invoke('echo:clicked', data),
  moved: (data) => ipcRenderer.invoke('echo:moved', data),
  scrolled: (data) => ipcRenderer.invoke('echo:scrolled', data),
  keyPressed: (data) => ipcRenderer.invoke('echo:keyPressed', data),
  dragged: (data) => ipcRenderer.invoke('echo:dragged', data),
});
