export async function downloadFile(name: string, loadBytes: () => Promise<ArrayBuffer>): Promise<void> {
  const bytes = await loadBytes()
  const url = URL.createObjectURL(new Blob([bytes]))
  const anchor = document.createElement('a')
  anchor.href = url
  anchor.download = name
  try {
    document.body.appendChild(anchor)
    anchor.click()
  } finally {
    anchor.remove()
    URL.revokeObjectURL(url)
  }
}
