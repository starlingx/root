# Patch Builder Utility

This utility will build patches based on .deb packages.

### Pre reqs

- Setup a build environment and build all packages/image
- Make code changes to your packages and build them

### Patch recipe schema

The patch builder requires the following tags in the input xml (or patch recipe)

```xml
<patch_recipe>
    <!-- Software Version -->
    <sw_version>1.0.0</sw_version>
    <!-- Component -->
    <component>starlingx</component>
    <!-- Summary: Short text to give a summary about the patch -->
    <summary>sample patch test</summary>
    <!-- Description: Patch description. Usually it has a list of fixes -->
    <description>Sample description</description>
    <!-- Install Instructions: Any instructions to be done before the patch installation -->
    <install_instructions>Sample instructions</install_instructions>
    <!-- Warnings: Any warnings that this patch can trigger -->
    <warnings>Sample warning</warnings>
    <!-- Reboot required: Y (Yes) or N (No) for in service patch -->
    <reboot_required>Y</reboot_required>
    <!-- Unremovable: Y (Yes)/ N (No), specifices if the patch can be removed -->
    <unremovable>N</unremovable>
    <!-- Patch Status: Supported values are DEV (development) and REL (released) -->
    <status>DEV</status>
    <!-- Requires: List of patches that are required by this patch -->
    <requires>
        <!--
        <id>PATCH_XYZ_01</id>
        <id>PATCH_XYZ_02</id>
        -->
    </requires>
    <semantics></semantics>
    <!--
        Activation scripts are scripts used to help with the upgrade of containerized solutions
        Leave blank if no scripts are required. Field should be full path to the files.
     -->
    <activation_scripts>
        <script>01-example.sh</script>
    </activation_scripts>
    <!--
        Pre and Post install hook scripts that are executed before/after patch installation.
        Leave blank if no scripts are required. Both fields require full path to the files.
    -->
    <pre_install>scripts/pre-install.sh</pre_install>
    <post_install>scripts/post-install.sh</post_install>
    <!-- List Packages to be included in the patch -->
    <stx_packages>
        <!-- Starlingx packages list -->
        <package>sysvinv</package>
        <package>linux</package>
        <package>linux-rt</package>
    </stx_packages>
    <!-- Binary packages list to be included in the patch (Packages that we download from 3rd party sources) -->
    <binary_packages>
        <!-- 3rd party packages list -->
        <package>curl</package>
    </binary_packages>
</patch_recipe>
```


### How to build a patch

- Enter the builder container
```bash
$ stx shell
$ cd $MY_REPO/build-tools/stx/patch
```

- Install py requirements
```bash
$ pip install -r requirements.txt
```

- Update the patch-recipe file. For examples please refer to the `EXAMPLES` folder.

- Update any pre/post script. For examples check refer to the `scripts` folder.

- Build your patch:

```bash
$ ./patch-builder --recipe EXAMPLES\patch-recipe-sample.xml
```
