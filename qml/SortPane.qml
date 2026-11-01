import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import QtQuick.Dialogs

ScrollView {
    id: pane
    required property var backend
    required property var theme
    clip: true
    FolderDialog {
        id: folderDialog
        property string settingKey: "source_folder"
        onAccepted: backend.setPath(settingKey, selectedFolder)
    }
    FileDialog {
        id: fileDialog
        fileMode: FileDialog.OpenFile
        nameFilters: ["Images (*.png *.jpg *.jpeg *.webp)"]
        onAccepted: backend.setPath("source_folder", selectedFile)
    }
    component PathInput: ColumnLayout {
        required property string title
        required property string settingKey
        Label { text: title; color: theme.muted }
        RowLayout {
            Layout.fillWidth: true
            TextField {
                text: backend.configuration[settingKey] || ""
                Layout.fillWidth: true; placeholderText: title
                onTextEdited: backend.setSetting(settingKey, text)
            }
            Button {
                text: "Browse…"
                onClicked: {
                    if (settingKey === "source_folder" && backend.configuration.exec_mode.indexOf("Single") >= 0) fileDialog.open()
                    else { folderDialog.settingKey = settingKey; folderDialog.open() }
                }
            }
        }
    }
    ColumnLayout {
        width: pane.availableWidth - 12; spacing: 16
        Label { text: "Sort your library"; color: theme.ink; font.bold: true; font.pixelSize: 23 }
        Label { text: "Choose your folders. Sorting runs in the background while you browse History."; color: theme.muted; Layout.fillWidth: true; wrapMode: Text.WordWrap }
        ComboBox {
            model: ["Automated Full Folder Batch", "Single File Review Mode"]
            currentIndex: Math.max(0, model.indexOf(backend.configuration.exec_mode))
            onActivated: backend.setSetting("exec_mode", currentText)
        }
        PathInput { title: "Source"; settingKey: "source_folder"; Layout.fillWidth: true }
        PathInput { title: "Destination library"; settingKey: "dest_folder"; Layout.fillWidth: true }
        Rectangle { Layout.fillWidth: true; implicitHeight: 1; color: theme.line }
        Label { text: backend.status; font.bold: true; font.pixelSize: 16; color: theme.ink; Layout.fillWidth: true; wrapMode: Text.WordWrap }
        Label { text: backend.sessionText; visible: backend.canResume; color: theme.muted; Layout.fillWidth: true; wrapMode: Text.WordWrap }
        Flow {
            Layout.fillWidth: true; spacing: 8
            Button { objectName: "sortStartButton"; text: "Start sorting"; visible: !backend.canResume; enabled: !backend.running && !backend.editing && !backend.loading; onClicked: backend.startSorting() }
            Button { text: "Resume"; visible: backend.canResume; onClicked: backend.resumeSorting() }
            Button { text: "Start over"; visible: backend.canResume; onClicked: backend.restartSorting() }
            Button { objectName: "sortSkipButton"; text: backend.skipPending ? "Skipping…" : "Skip current image"; enabled: backend.canSkip; onClicked: backend.skipSorting() }
            Button { text: "Stop"; enabled: backend.running; onClicked: backend.stopSorting() }
        }
        ProgressBar { Layout.fillWidth: true; from: 0; to: 1; value: backend.runProgress }
        Label { text: backend.details; color: theme.muted; Layout.fillWidth: true; wrapMode: Text.WordWrap }
        RowLayout {
            spacing: 12; Layout.fillWidth: true
            Repeater {
                model: [{label: "Your image", source: backend.latestLocal}, {label: "Database match", source: backend.latestMatch}]
                ColumnLayout {
                    required property var modelData
                    Layout.fillWidth: true
                    Label { text: modelData.label; color: theme.muted; font.pixelSize: 12 }
                    Rectangle {
                        Layout.fillWidth: true; implicitHeight: 180; radius: 5; color: theme.panel; border.color: theme.line
                        Image { id: image; anchors.fill: parent; anchors.margins: 8; source: modelData.source; sourceSize: Qt.size(256, 256); asynchronous: true; fillMode: Image.PreserveAspectFit }
                        Label { anchors.centerIn: parent; color: theme.muted; text: "No preview"; visible: !modelData.source || image.status === Image.Error }
                        BusyIndicator { anchors.centerIn: parent; running: image.status === Image.Loading; width: 32; height: 32 }
                    }
                }
            }
        }
        Label { text: "Matching mode: " + backend.configuration.matching_mode + " · settings changes apply to the next batch"; color: theme.muted; Layout.fillWidth: true; wrapMode: Text.WordWrap; font.pixelSize: 12 }
    }
}
