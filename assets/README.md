# Brand assets

| File | Use | Notes |
|------|-----|-------|
| `banner.webp` | README header (2000×667) | Referenced by an absolute raw.githubusercontent URL so it renders on PyPI too. |
| `social-preview.png` | GitHub social preview (1280×640) | Settings → General → Social preview → *Upload an image*. GitHub accepts only PNG, JPG or GIF under 1 MB, and crops to 2:1, so this is the banner letterboxed rather than the WebP original. Regenerate with `scripts/make_social_preview.py`. |
