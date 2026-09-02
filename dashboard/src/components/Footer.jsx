// Rendered once per page (App.jsx), below <main>. Attribution + contact,
// kept deliberately small and muted so it never competes with the screens
// above it.
export default function Footer() {
  return (
    <footer className="border-t border-line px-6 py-4 text-xs text-muted">
      <p>Built by Ekansh Chaurasiya</p>
      <p className="mt-1 flex flex-wrap gap-x-4 gap-y-1">
        <a
          href="https://github.com/PythonScript32/wapsi"
          target="_blank"
          rel="noopener noreferrer"
          className="hover:text-white"
        >
          GitHub: github.com/PythonScript32/wapsi
        </a>
        <a
          href="https://linkedin.com/in/ekanshchaurasiya"
          target="_blank"
          rel="noopener noreferrer"
          className="hover:text-white"
        >
          LinkedIn: linkedin.com/in/ekanshchaurasiya
        </a>
        <a href="mailto:ekanshchaurasiya3@gmail.com" className="hover:text-white">
          ekanshchaurasiya3@gmail.com
        </a>
      </p>
      <p className="mt-1">Razorpay AI Buildathon · Track 3 — AI Revenue Recovery</p>
    </footer>
  )
}
