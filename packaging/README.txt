POD Batch Splitter — Branch setup
=================================

1. Unzip this entire folder to e.g. D:\POD_Splitter\
   (You need write access to that folder — no Python install required.)

2. Double-click:  Start POD Splitter.bat
   Leave the window open while scanning.

3. In Kodak Capture Pro, set the PDF export/output folder to:
      <this folder>\POD_System\1_Input

   Example: D:\POD_Splitter\POD_System\1_Input

4. Scan as usual — one multi-page PDF per batch (PODs + invoices mixed).

5. Split waybill PDFs appear in:
      <this folder>\POD_System\2_Output
   Each file is named after the waybill barcode, e.g. LDLS924870.pdf

6. Upload files from 2_Output to your server (FileZilla or company process).

Folders
-------
  POD_System\1_Input    <- Kodak saves scan batches here
  POD_System\2_Output   <- Split waybill PDFs (upload these)
  POD_System\3_Archive  <- Original batches (backup)
  POD_System\4_Errors   <- Pages that could not be processed
  processing_log.txt    <- Activity log

Support
-------
  If splitting fails, check 4_Errors and processing_log.txt, then contact IT.
