### **🚨 Urgent Compatibility Notice**

**Compatibility has changed!** The current workflow uses a Generic FBX based on 3ds Max scale units. As a result, models imported through the old Blender workflow and models imported through the current workflow can differ by a factor of **2.54**. Models created with the old workflow should be imported into Blender again, imported with the scale set to **2.54×**, checked until they match the intended reference size, and then exported again. This prevents the exported model from becoming offset.

**We also do not know how to make this perfect; there is no going back.**

- Added Scaling Mode
- Added RE6 MOD scanning
- Fixed MAX Export UV Map 2
- Added backup mechanisms for Release notes
- Fixed Blender hierarchy per import and export
- Fixed Legacy4 FVF weight consistency per export
- Max Exclusive: Merged duplicate same-name bones to prevent bone/MESH binding errors
- Enhanced 3D software connectivity; 3ds Max startup .py files are always disabled.
- Old update checkers always report an update; newer update-check mechanisms are enforced
- MAX/Blender scene nodes one time no cache policy that might mislead; FBX exclusively decides geometry
- Fixed normal anomalies in RE6 FVF BB424024/BB424025
- FVF B0983013/14, 0CB68015/16, A8FAB018/19, and D877801B bone-index padding exports as 00 Mesh Teeth vanished issue fixed
- Corrected FVF CBF6C01A import/source-truth/export handling to one little-endian u16 bone-index field at offsets 6..7
- Fixed MOD export normal fidelity: the exported normal representation, values, and count now remain consistent with the source FBX
- Removed ufbx as a required dependency and made it optional to avoid the growing number of error cases
- Non-conforming bone names no longer block export and are discarded
