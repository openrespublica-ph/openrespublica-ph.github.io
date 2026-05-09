document.addEventListener('DOMContentLoaded', () => {
    const params = new URLSearchParams(window.location.search);
    const hash = params.get('hash');
    const btn = document.getElementById('verifyBtn');

    if (hash) {
        document.getElementById('hashInput').value = hash;
        checkHash(hash);
    }

    btn.addEventListener('click', () => checkHash());
});

async function checkHash(inputHash, retryCount = 0) {
    const hash = (inputHash || document.getElementById('hashInput').value).trim();
    if (!hash) return;

    const vBox = document.getElementById('verifiedBox');
    const eBox = document.getElementById('errorBox');
    const load = document.getElementById('loading');
    const details = document.getElementById('details');
    
    vBox.style.display = 'none'; 
    eBox.style.display = 'none'; 
    load.style.display = 'block';

    try {
        // Cache-buster ensures we don't see old data
        const response = await fetch(`records/${hash}.json?t=${Date.now()}`);
        
        if (!response.ok) throw new Error("Not found");
        
        const data = await response.json();
        
        details.innerHTML = `
            <hr>
            <strong>Signer:</strong> ${data.signer}<br>
            <strong>Position:</strong> ${data.position}<br>
            <strong>Control No:</strong> ${data.control_number}<br>
            <strong>Timestamp:</strong> ${data.timestamp}<br>
            <strong>immudb Tx:</strong> <mark>#${data.immudb_transaction_id}</mark><br>
            <strong>Fingerprint:</strong> <code>${data.sha256_hash}</code>
        `;
        
        vBox.style.display = 'block';
    } catch (e) {
        if (retryCount < 1) {
            load.innerHTML = "<ins>⏳ Node propagation lag... retrying in 10s</ins>";
            setTimeout(() => checkHash(hash, retryCount + 1), 10000);
        } else {
            document.getElementById('errorHash').innerText = hash;
            eBox.style.display = 'block';
        }
    } finally {
        load.style.display = 'none';
    }
}
