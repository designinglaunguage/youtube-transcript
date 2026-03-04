export default {
  async fetch(request) {
    if (request.method === 'OPTIONS') {
      return new Response(null, {
        headers: {
          'Access-Control-Allow-Origin': '*',
          'Access-Control-Allow-Methods': 'GET, POST, OPTIONS',
          'Access-Control-Allow-Headers': '*',
        }
      });
    }

    const url = new URL(request.url);
    const targetUrl = url.searchParams.get('url');

    if (!targetUrl) {
      return new Response('Missing url parameter', { status: 400 });
    }

    try {
      // Preserve original request headers (keeps cookies, auth tokens from youtube-transcript-api)
      const headers = new Headers(request.headers);
      // Remove headers that Cloudflare Workers should not forward
      headers.delete('host');
      headers.delete('cf-connecting-ip');
      headers.delete('cf-ray');
      headers.delete('cf-ipcountry');
      // Override User-Agent with a browser UA
      headers.set('User-Agent', 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36');
      headers.set('Accept-Language', headers.get('Accept-Language') || 'ko-KR,ko;q=0.9,en-US;q=0.8,en;q=0.7');
      // Merge CONSENT cookie with any existing cookies
      const consentCookie = 'CONSENT=PENDING+987; SOCS=CAISHAgCEhJnd3NfMjAyNDAxMDEtMF9SQzIaAmVuIAEaBgiA0JCuBg';
      const existingCookie = headers.get('Cookie');
      if (existingCookie) {
        headers.set('Cookie', consentCookie + '; ' + existingCookie);
      } else {
        headers.set('Cookie', consentCookie);
      }

      const fetchOptions = {
        method: request.method,
        headers,
        redirect: 'follow',
      };

      // Forward body for POST requests
      if (request.method === 'POST') {
        fetchOptions.body = await request.arrayBuffer();
      }

      const response = await fetch(targetUrl, fetchOptions);
      const body = await response.arrayBuffer();

      return new Response(body, {
        status: response.status,
        headers: {
          'Content-Type': response.headers.get('Content-Type') || 'text/html',
          'Access-Control-Allow-Origin': '*',
        }
      });
    } catch (err) {
      return new Response(JSON.stringify({ error: err.message }), {
        status: 502,
        headers: { 'Content-Type': 'application/json' }
      });
    }
  }
};
