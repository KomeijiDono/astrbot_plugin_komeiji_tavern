export const metadata = {
  title: "Komeiji Request Relay",
  description: "Short-lived request pages for model capability testing.",
};

export default function Home() {
  return (
    <main className="status-shell">
      <section className="status-card" aria-labelledby="service-title">
        <div className="status-line">
          <span className="status-dot" aria-hidden="true" />
          <span>Service online</span>
        </div>
        <h1 id="service-title">Komeiji Request Relay</h1>
        <p>
          This service hosts short-lived model requests behind unguessable
          links. It does not provide a directory, request browser, or public
          management interface.
        </p>
        <p className="privacy-note">
          A temporary link is an access credential. Do not share it. Expired or
          deleted requests return 404.
        </p>
        <a href="/health">Health status</a>
      </section>
    </main>
  );
}
