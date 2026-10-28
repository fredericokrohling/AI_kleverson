const campo = document.getElementById("url");

chrome.storage.sync.get("backendUrl").then(({ backendUrl }) => {
  campo.value = backendUrl || "";
});

document.getElementById("salvar").addEventListener("click", async () => {
  const valor = campo.value.trim().replace(/\/$/, "");
  if (!/^https:\/\/[^/]+\.ts\.net$/.test(valor)) {
    document.getElementById("salvo").textContent = "Use o endereço https://...ts.net do console.";
    return;
  }
  await chrome.storage.sync.set({ backendUrl: valor });
  document.getElementById("salvo").textContent = "Salvo.";
});

document.getElementById("microfone").addEventListener("click", async () => {
  const estado = document.getElementById("estado-mic");
  try {
    const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
    stream.getTracks().forEach((t) => t.stop());
    estado.textContent = "Autorizado.";
  } catch (e) {
    estado.textContent = `Não autorizado: ${e.message}`;
  }
});
