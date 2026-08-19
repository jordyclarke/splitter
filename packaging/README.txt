POD Batch Splitter — Branch setup
=================================

Workflow change (from tomorrow)
-------------------------------
  OLD: Staff manually split PODs in Capture Pro before hitting Output.
  NEW: Scan the full batch (PODs + invoices mixed), then Output once as
       ONE multi-page PDF. The splitter does the waybill split automatically.

  Keep process_existing = false in settings.ini so old manually-split PDFs
  already in the Kodak folders are ignored. Only new batch PDFs created
  after the splitter is running will be processed.

1. Unzip this entire folder to e.g. D:\POD_Splitter\
   (You need write access to that folder — no Python install required.)

2. Configure Kodak watch folder (recommended — no Capture Pro changes):
      a. Copy  settings.ini.example  to  settings.ini
      b. Edit settings.ini — set "folder" to your Kodak PODS path, e.g.:
         C:\Users\PolokwaneAdmin\Documents\IMPORTANT DOCMENTS\SCANNING\POD SCANS\PODS
      c. Leave recursive = true, archive_source = false, process_existing = false

   Alternative (old method): skip settings.ini and point Kodak PDF output to:
      <this folder>\POD_System\1_Input

3. Double-click:  Start POD Splitter.bat
   Leave the window open BEFORE you start scanning tomorrow.

4. In Kodak Capture Pro — scan all pages, do NOT split manually, then Output.
   One multi-page Searchable PDF per batch (mixed waybills + invoice pages).

5. Split waybill PDFs appear in a new folder under:
      <this folder>\POD_System\2_Output\<batch name>\
   Example: POD_System\2_Output\SBAIN30762606\LDLS926241.pdf
   Each batch gets its own folder so today's scans don't mix with yesterday's.

6. Upload files from that batch folder to your server (FileZilla or company process).

Folders
-------
  POD_System\2_Output   <- One subfolder per batch (upload from there)
  POD_System\3_Archive  <- Original batches (only when archive_source = true)
  POD_System\4_Errors   <- Batches that could not be processed
  processing_log.txt    <- Activity log
  processed_batches.txt <- Tracks batches already split (do not delete)

Support
-------
  If splitting fails, check 4_Errors and processing_log.txt, then contact IT.
