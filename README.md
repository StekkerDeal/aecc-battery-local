# Sunpura-Local-TCP
Control the sunpura s2400 over local tcp (No usage of the cloud)

Exposes following sensors: 
- AC charging power
- AC discharge power
- Battery SOC
- Power setpoint. (- is discharge, + is charging)

<img width="362" height="566" alt="Screenshot 2026-03-29 210640" src="https://github.com/user-attachments/assets/2e72fe45-7b62-403f-b38e-85de6e99f6a6" />

Internval update can be set to about 6 to 8 seconds for automations.


Look for the local battery IP adress in your router/AP. Input this in the setup window of the plugin.
The port for control is 8080.

Only ONE local device can control the battery at the same time.

Upload files under: custom_components/aecc_local
