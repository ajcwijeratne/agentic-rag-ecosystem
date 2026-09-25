# Document templates

`wijerco-reference.docx` holds the styles every Deliverables DOCX is built from:
Open Sans throughout, pine headings, A4 with 2.2 cm margins. Open it in Word to
adjust a style (Heading 1, Normal, Quote, List Bullet, Title, Subtitle) and save;
the next render picks it up. Keep the body empty.

Regenerate it from code with:

    python -c "from media.doc_render import build_reference_template; build_reference_template()"

Slide decks are drawn in code (`media/doc_render.py`, `render_pptx`), so there is
no PowerPoint template to maintain.
