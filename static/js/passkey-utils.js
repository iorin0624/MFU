(function () {
  const base64UrlToArrayBuffer = (base64url) => {
    const padding = "=".repeat((4 - (base64url.length % 4)) % 4);
    const base64 = (base64url + padding).replace(/-/g, "+").replace(/_/g, "/");
    const binary = atob(base64);
    const buffer = new Uint8Array(binary.length);
    for (let i = 0; i < binary.length; i++) {
      buffer[i] = binary.charCodeAt(i);
    }
    return buffer.buffer;
  };

  const arrayBufferToBase64Url = (buffer) => {
    const bytes = new Uint8Array(buffer);
    let binary = "";
    bytes.forEach((b) => {
      binary += String.fromCharCode(b);
    });
    return btoa(binary).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/g, "");
  };

  const decodeRequestOptions = (options) => {
    const decoded = { ...options };
    decoded.challenge = base64UrlToArrayBuffer(options.challenge);
    if (options.allowCredentials) {
      decoded.allowCredentials = options.allowCredentials.map((cred) => ({
        ...cred,
        id: base64UrlToArrayBuffer(cred.id),
      }));
    }
    return decoded;
  };

  const decodeCreationOptions = (options) => {
    const decoded = { ...options };
    decoded.challenge = base64UrlToArrayBuffer(options.challenge);
    if (options.user && options.user.id) {
      decoded.user = { ...options.user, id: base64UrlToArrayBuffer(options.user.id) };
    }
    if (options.excludeCredentials) {
      decoded.excludeCredentials = options.excludeCredentials.map((cred) => ({
        ...cred,
        id: base64UrlToArrayBuffer(cred.id),
      }));
    }
    return decoded;
  };

  const serializeRegistrationCredential = (credential) => ({
    id: credential.id,
    rawId: arrayBufferToBase64Url(credential.rawId),
    type: credential.type,
    response: {
      clientDataJSON: arrayBufferToBase64Url(credential.response.clientDataJSON),
      attestationObject: arrayBufferToBase64Url(credential.response.attestationObject),
    },
  });

  const serializeAuthenticationCredential = (credential) => ({
    id: credential.id,
    rawId: arrayBufferToBase64Url(credential.rawId),
    type: credential.type,
    response: {
      clientDataJSON: arrayBufferToBase64Url(credential.response.clientDataJSON),
      authenticatorData: arrayBufferToBase64Url(credential.response.authenticatorData),
      signature: arrayBufferToBase64Url(credential.response.signature),
      userHandle: credential.response.userHandle
        ? arrayBufferToBase64Url(credential.response.userHandle)
        : null,
    },
  });

  window.PasskeyUtils = {
    base64UrlToArrayBuffer,
    arrayBufferToBase64Url,
    decodeRequestOptions,
    decodeCreationOptions,
    serializeRegistrationCredential,
    serializeAuthenticationCredential,
  };
})();
