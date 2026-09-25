type JsonCredentialDescriptor = { id: string; type: PublicKeyCredentialType; transports?: AuthenticatorTransport[] }

type CreationOptions = Omit<PublicKeyCredentialCreationOptions, 'challenge' | 'user' | 'excludeCredentials'> & {
  challenge: string
  user: Omit<PublicKeyCredentialUserEntity, 'id'> & { id: string }
  excludeCredentials?: JsonCredentialDescriptor[]
}

type RequestOptions = Omit<PublicKeyCredentialRequestOptions, 'challenge' | 'allowCredentials'> & {
  challenge: string
  allowCredentials?: JsonCredentialDescriptor[]
}

function decode(value: string): Uint8Array {
  const normalized = value.replace(/-/g, '+').replace(/_/g, '/')
  const padded = normalized.padEnd(Math.ceil(normalized.length / 4) * 4, '=')
  return Uint8Array.from(atob(padded), char => char.charCodeAt(0))
}

function encode(value: ArrayBuffer | null): string | null {
  if (!value) return null
  const bytes = new Uint8Array(value)
  let binary = ''
  bytes.forEach(byte => { binary += String.fromCharCode(byte) })
  return btoa(binary).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/g, '')
}

function descriptors(values?: JsonCredentialDescriptor[]): PublicKeyCredentialDescriptor[] | undefined {
  return values?.map(value => ({ ...value, id: decode(value.id) }))
}

export function passkeysSupported(): boolean {
  return window.isSecureContext && 'PublicKeyCredential' in window && !!navigator.credentials
}

export async function createPasskey(options: CreationOptions): Promise<Record<string, unknown>> {
  const credential = await navigator.credentials.create({
    publicKey: {
      ...options,
      challenge: decode(options.challenge),
      user: { ...options.user, id: decode(options.user.id) },
      excludeCredentials: descriptors(options.excludeCredentials),
    },
  }) as PublicKeyCredential | null
  if (!credential) throw new Error('パスキーの登録がキャンセルされました。')
  const response = credential.response as AuthenticatorAttestationResponse
  return {
    id: credential.id, rawId: encode(credential.rawId), type: credential.type,
    authenticatorAttachment: credential.authenticatorAttachment,
    clientExtensionResults: credential.getClientExtensionResults(),
    response: {
      clientDataJSON: encode(response.clientDataJSON),
      attestationObject: encode(response.attestationObject),
      transports: response.getTransports?.() ?? [],
    },
  }
}

export async function getPasskey(options: RequestOptions): Promise<Record<string, unknown>> {
  const credential = await navigator.credentials.get({
    publicKey: {
      ...options,
      challenge: decode(options.challenge),
      allowCredentials: descriptors(options.allowCredentials),
    },
  }) as PublicKeyCredential | null
  if (!credential) throw new Error('パスキーによる認証がキャンセルされました。')
  const response = credential.response as AuthenticatorAssertionResponse
  return {
    id: credential.id, rawId: encode(credential.rawId), type: credential.type,
    authenticatorAttachment: credential.authenticatorAttachment,
    clientExtensionResults: credential.getClientExtensionResults(),
    response: {
      clientDataJSON: encode(response.clientDataJSON),
      authenticatorData: encode(response.authenticatorData),
      signature: encode(response.signature), userHandle: encode(response.userHandle),
    },
  }
}
