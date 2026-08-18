POD Batch Splitter — Branch setup
=================================

1. Unzip this entire folder to e.g. D:\POD_Splitter\
   (You need write access to that folder — no Python install required.)

2. Configure Kodak watch folder (recommended — no Capture Pro changes):
      a. Copy  settings.ini.example  to  settings.ini
      b. Edit settings.ini — set "folder" to your Kodak PODS path, e.g.:
         C:\Users\PolokwaneAdmin\Documents\IMPORTANT DOCMENTS\SCANNING\POD SCANS\PODS
      c. Leave recursive = true and archive_source = false

   Alternative (old method): skip settings.ini and point Kodak PDF output to:
      <this folder>\POD_System\1_Input

3. Double-click:  Start POD Splitter.bat
   Leave the window open while scanning.

4. Scan as usual in Kodak Capture Pro — one multi-page PDF per batch.

5. Split waybill PDFs appear in:
      <this folder>\POD_System\2_Output
   Each file is named after the waybill barcode, e.g. LDLS924870.pdf

6. Upload files from 2_Output to your server (FileZilla or company process).

Folders
-------
  POD_System\2_Output   <- Split waybill PDFs (upload these)
  POD_System\3_Archive  <- Original batches (only when archive_source = true)
  POD_System\4_Errors   <- Batches that could not be processed
  processing_log.txt    <- Activity log
  processed_batches.txt <- Tracks batches already split (do not delete)

Support
-------
  If splitting fails, check 4_Errors and processing_log.txt, then contact IT.
