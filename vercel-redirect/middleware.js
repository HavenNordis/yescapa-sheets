/**
 * Haven Nordis — redirect curto (r.havennordis.com), no Vercel.
 *
 * Edge Middleware: recebe r.havennordis.com/{slug} e faz 302 para o URL Tally
 * longo pré-preenchido, que o cron do Railway escreveu no Edge Config.
 *
 *   /3461997-tiago-cascais      → https://forms.havennordis.com/pre-check-in?name=...&ref=3461997&...
 *   /3461997-tiago-cascais-en   → https://forms.havennordis.com/pre-check-in-en?...
 *
 * É deliberadamente "burro": toda a inteligência (slug + URL longo) vem do
 * Edge Config. Não tem credenciais nem fala com o Google. Resolve SÓ pela chave
 * do slug — o nome no slug é cosmético, só o que está no store conta.
 */

import { get } from "@vercel/edge-config";

export const config = { matcher: "/:path*" };

const SITE = "https://havennordis.com";

export default async function middleware(request) {
  const url = new URL(request.url);
  let slug = decodeURIComponent(url.pathname)
    .replace(/^\/+/, "")
    .replace(/\/+$/, "");

  // Raiz ou favicon → site institucional (nada de 404 feio).
  if (!slug || slug === "favicon.ico") {
    return Response.redirect(SITE, 302);
  }

  // O slug no store está sempre em minúsculas.
  slug = slug.toLowerCase();

  let destino = null;
  try {
    destino = await get(slug);
  } catch (e) {
    destino = null;
  }

  if (destino) {
    // 302 (temporário) de propósito: se o URL Tally mudar, basta reescrever o
    // store — nada fica em cache "para sempre" no browser do hóspede.
    return Response.redirect(destino, 302);
  }

  return new Response(pagina404(), {
    status: 404,
    headers: { "content-type": "text/html; charset=utf-8" },
  });
}

function pagina404() {
  return `<!doctype html>
<html lang="pt">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Haven Nordis</title>
<style>
  body{margin:0;min-height:100vh;display:flex;align-items:center;justify-content:center;
       background:#03232D;color:#fff;font-family:Helvetica,Arial,sans-serif;text-align:center;padding:24px}
  .card{max-width:420px}
  h1{font-size:20px;font-weight:500;margin:0 0 12px}
  p{font-size:15px;line-height:1.5;opacity:.85;margin:0 0 20px}
  a{display:inline-block;background:#fff;color:#03232D;text-decoration:none;
    padding:12px 28px;border-radius:6px;font-weight:500}
</style>
</head>
<body>
  <div class="card">
    <h1>Link não encontrado</h1>
    <p>Este link de pré-check-in não é válido ou expirou. Se recebeu este link
       da Haven Nordis, responda à mensagem que lhe enviámos e nós reenviamos.</p>
    <a href="${SITE}">Ir para havennordis.com</a>
  </div>
</body>
</html>`;
}
