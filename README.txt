1. Copy path to your file
2. Open Command Prompt, run path your file for example " cd "C:\Users\Potato\Desktop\LOL_Skin_Modder_VN_v0.0.3" "
3. Run "pip install -r requirements.txt"
4. Run "python main.py"
5. Follow instruction in the terminal 

--Problems--:
Hash problem
Launch csmod too fast despite skin not installing completely:
2025-08-08 15:04:02,972 - __main__ - INFO - Installing (171/171): Zyra
2025-08-08 15:04:03,122 - __main__ - INFO - Wrote installed hash afa3d9d4
2025-08-08 15:04:03,122 - __main__ - INFO - Auto-install finished. Total installed skins (approx): 1843

Doesnt launch csmod upon the condition of finish installing and running another instance

Still installing despite need install = false
2025-08-08 15:05:34,867 - __main__ - INFO - Installed hash (file)=afa3d9d4 computed=afa3d9d4 needs_install=False
2025-08-08 15:05:34,870 - __main__ - INFO - Starting auto-install of all champion skins (skip chromas=True)